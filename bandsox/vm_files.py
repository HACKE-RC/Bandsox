"""File-transfer and inspection operations for MicroVM (mixin).

Split out of vm.py; combined back into the MicroVM class via inheritance.
These methods rely on transport/vsock helpers (send_request, _fastread_remote,
exec_command, ...) resolved on `self` through the MicroVM MRO.
"""
import os
import json
import time
import uuid
import base64
import tempfile
import subprocess
import logging
from pathlib import Path

from .vm_common import (
    FastIOError,
    _DIRECT_TEXT_WRITE_MAX_BYTES,
    _SERIAL_WRITE_CHUNK_SIZE,
    _DEBUGFS_FULL_FILE_FALLBACK_LOG_THRESHOLD,
)

logger = logging.getLogger(__name__)


class _FileOpsMixin:
    def _has_debugfs_rootfs(self) -> bool:
        rootfs_path = getattr(self, "rootfs_path", None)
        return bool(rootfs_path and os.path.exists(rootfs_path))

    def _debugfs_download_file(self, remote_path: str, local_path: str) -> None:
        """Read a file directly from the ext4 rootfs with debugfs.

        This path is used when the guest agent is unavailable but the rootfs
        image is still accessible on the host. We pause/resume the VM
        best-effort when we own a live Firecracker socket to reduce the risk
        of reading a mutating filesystem.
        """
        if not self._has_debugfs_rootfs():
            raise Exception("debugfs fallback unavailable: rootfs_path is missing")

        rootfs_path = os.path.abspath(self.rootfs_path)
        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        remote_quoted = remote_path.replace('"', '\\"')
        paused = False
        try:
            if getattr(self, "socket_path", None) and os.path.exists(self.socket_path):
                try:
                    self.pause()
                    paused = True
                except Exception as exc:
                    logger.warning(f"Failed to pause VM before debugfs read: {exc}")

            cmd = [
                "debugfs",
                "-R",
                f"dump -p \"{remote_quoted}\" \"{local_path}\"",
                rootfs_path,
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
            )
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            combined = f"{stderr}\n{stdout}".lower()
            if proc.returncode != 0 or any(
                marker in combined
                for marker in (
                    "file not found",
                    "not found by ext2_lookup",
                    "no such file",
                    "does not exist",
                )
            ):
                detail = stderr or stdout or f"debugfs exited with {proc.returncode}"
                raise Exception(detail)
        finally:
            if paused:
                try:
                    self.resume()
                except Exception as exc:
                    logger.warning(f"Failed to resume VM after debugfs read: {exc}")

    def get_file_contents(
        self,
        path: str,
        offset: int = 0,
        limit: int = 0,
        show_line_numbers: bool = False,
        show_header: bool = True,
        show_footer: bool = True,
    ) -> str:
        """Reads the contents of a file inside the VM.

        Args:
            path: File path in VM
            offset: Lines to skip from beginning (0 = start at line 1)
            limit: Max lines to return (0 = unlimited)
            show_line_numbers: Prefix each line with "N\\t"
            show_header: If offset>0, show "... skipped N lines" header
            show_footer: If limit>0 and more lines remain, show "... N lines left" footer

        The agent reports total line count alongside the content, so
        offset/limit slicing happens server-side even when header/footer is
        requested — the full file is never transferred over vsock/serial
        just for decoration.
        """
        agent_error = None
        if self.agent_ready:
            import base64
            import hashlib

            raw_bytes = None
            raw_bytes_full_file = False

            # Fast-read RPC: when the local process doesn't own the vsock
            # listener (e.g. athena's webui talking to a detached runner),
            # ask the runner to do the read via its in-namespace listener
            # and stream the bytes back over a side-channel UDS. This is
            # what unlocks vsock speed for ManagedMicroVM callers.
            if self.vsock_listener is None and offset == 0 and limit == 0 and not show_line_numbers:
                fr_path = self._fastread_socket_path_for_remote()
                if fr_path:
                    # Briefly poll for the socket — closes the race where
                    # the runner is still binding its fastread server when
                    # athena issues the first read after restore_vm. Without
                    # this, the first athena read loses to the listener-bind
                    # and silently falls back to the slow serial path.
                    deadline = time.time() + 1.5
                    while not os.path.exists(fr_path) and time.time() < deadline:
                        time.sleep(0.025)
                    if os.path.exists(fr_path):
                        # Retry once on transient errors (saturated, listener_down,
                        # connection reset). One retry hides multiplexer/runner
                        # blips from athena's UI without masking real failures
                        # like not_found.
                        for attempt in range(2):
                            try:
                                raw_bytes = self._fastread_remote(fr_path, path)
                                raw_bytes_full_file = True
                                # Empty buffer with no error usually means
                                # the listener slot fired before the agent
                                # actually uploaded — a race. Treat as a
                                # soft failure on first attempt so we
                                # retry; on second, fall through to serial
                                # so the caller doesn't get a phantom-empty.
                                if not raw_bytes and attempt == 0:
                                    logger.info(
                                        f"FastRead returned 0 bytes for {path}; retrying"
                                    )
                                    raw_bytes = None
                                    time.sleep(0.05)
                                    continue
                                if not raw_bytes:
                                    logger.info(
                                        f"FastRead still 0 bytes for {path}; falling through to serial"
                                    )
                                    raw_bytes = None
                                break
                            except FastIOError as exc:
                                if attempt == 0 and exc.code in (
                                    "saturated", "listener_down", "timeout", "internal"
                                ):
                                    logger.info(
                                        f"FastRead transient {exc.code} for {path}; retrying"
                                    )
                                    time.sleep(0.05)
                                    continue
                                logger.info(
                                    f"FastRead RPC failed for {path}: {exc}"
                                )
                                raw_bytes = None
                                break
                            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                                if attempt == 0:
                                    logger.info(
                                        f"FastRead transient socket error for {path}: {exc}; retrying"
                                    )
                                    time.sleep(0.05)
                                    continue
                                logger.info(
                                    f"FastRead RPC failed for {path}: {exc}"
                                )
                                raw_bytes = None
                                break
                            except Exception as exc:
                                logger.info(
                                    f"FastRead RPC failed for {path} via {fr_path}: {exc}"
                                )
                                raw_bytes = None
                                break

            # Vsock fast path: ask the guest to upload the file directly into
            # an in-memory buffer on the listener. We do NOT synchronously
            # wait for the agent's serial "exit" event — that would queue
            # N exits through the single serial console under heavy parallel
            # reads. Instead we wait on the listener's done-event, which
            # fires the moment the bytes have all landed. The exit/status
            # serial events arrive asynchronously and clean up callbacks
            # via the existing dispatch loop.
            vsock_ready = (
                raw_bytes is None
                and self.vsock_enabled
                and self.vsock_listener is not None
                and getattr(self, "vsock_port", None)
                and offset == 0
                and limit == 0
                and not show_line_numbers
            )
            if vsock_ready:
                cmd_id = str(uuid.uuid4())
                slot = self.vsock_listener.register_pending_buffer(cmd_id)

                # Lightweight callback set so _handle_stdout_line can clean
                # up event_callbacks when the agent's exit event eventually
                # arrives. We don't block on it.
                vsock_error_holder = {"err": None, "exited_nonzero": False}

                def _vsock_on_error(msg, _e=vsock_error_holder, _slot=slot):
                    _e["err"] = msg
                    # Wake the waiter — guest reported failure (e.g. file not
                    # found) and will never upload, so don't block 60s.
                    _slot["done"].set()

                def _vsock_on_exit(code, _e=vsock_error_holder, _slot=slot):
                    if code != 0:
                        _e["exited_nonzero"] = True
                        _slot["done"].set()

                with self._event_callbacks_lock:
                    self.event_callbacks[cmd_id] = {
                        "on_stdout": None,
                        "on_stderr": None,
                        "on_file_content": None,
                        "on_file_chunk": None,
                        "on_file_complete": None,
                        "on_dir_list": None,
                        "on_file_info": None,
                        "on_status": None,
                        "on_exit": _vsock_on_exit,
                        "on_error": _vsock_on_error,
                    }

                payload_dict = {
                    "id": cmd_id,
                    "type": "read_file",
                    "path": path,
                    "use_vsock": True,
                    "vsock_port": self.vsock_port,
                }
                try:
                    self._write_to_agent(json.dumps(payload_dict) + "\n")
                    woke = slot["done"].wait(timeout=60)
                    if vsock_error_holder["err"]:
                        # Guest reported an error (e.g. file not found).
                        # Don't fall back to serial — it'd hit the same error.
                        raise Exception(vsock_error_holder["err"])
                    if not woke:
                        raise TimeoutError(f"Vsock read of {path} timed out")
                    if slot["error"]:
                        raise Exception(slot["error"])
                    if vsock_error_holder["exited_nonzero"] and not slot["buf"]:
                        raise Exception("Agent exited non-zero before upload")
                    raw_bytes = bytes(slot["buf"])
                    raw_bytes_full_file = True
                except Exception as exc:
                    # Use INFO so we can see in the bench/server log when the
                    # fast path is being skipped or failing — without this, a
                    # silent fallback to serial looks identical to "vsock
                    # never tried".
                    logger.info(f"Vsock read fast-path failed for {path}: {exc}")
                    raw_bytes = None
                    raw_bytes_full_file = False
                finally:
                    self.vsock_listener.unregister_pending_buffer(cmd_id)

            agent_total_lines = None
            if raw_bytes is None:
                result = {
                    "mode": None,
                    "content": None,
                    "chunks": bytearray(),
                    "checksum": None,
                    "total_size": None,
                }

                def on_file_content(c):
                    result["mode"] = "single"
                    result["content"] = c

                def on_file_chunk(data, offset, size):
                    if result["mode"] is None:
                        result["mode"] = "chunked"
                    result["chunks"].extend(base64.b64decode(data))

                def on_file_complete(total_size, checksum):
                    result["total_size"] = total_size
                    result["checksum"] = checksum

                # Retry the agent read up to 3 times on empty/missing content
                # before giving up. The agent under heavy concurrency can
                # emit a file_content event with empty content (or drop the
                # event entirely after the retry-storm rebuilt the connection)
                # — that surfaces as a phantom empty file in athena's UI.
                # We detect "empty result with no error" and try again with a
                # fresh result dict.
                last_attempt_was_empty = False
                for serial_attempt in range(3):
                    # Fresh result dict per attempt so a stale empty from a
                    # prior attempt doesn't poison this one.
                    result = {
                        "mode": None,
                        "content": None,
                        "chunks": bytearray(),
                        "checksum": None,
                        "total_size": None,
                        "agent_total_lines": None,
                    }
                    cmd_id = str(uuid.uuid4())
                    def on_file_content(c, _r=result, _cid=cmd_id):
                        _r["mode"] = "single"
                        _r["content"] = c
                        _tls = self._agent_total_lines_for(_cid)
                        if _tls is not None:
                            _r["agent_total_lines"] = _tls
                    def on_file_chunk(data, offset_, size, _r=result):
                        if _r["mode"] is None:
                            _r["mode"] = "chunked"
                        _r["chunks"].extend(base64.b64decode(data))
                    def on_file_complete(total_size, checksum, _r=result, _cid=cmd_id):
                        _r["total_size"] = total_size
                        _r["checksum"] = checksum
                        _tls = self._agent_total_lines_for(_cid)
                        if _tls is not None:
                            _r["agent_total_lines"] = _tls

                    try:
                        self._send_request_with_id(
                            cmd_id,
                            "read_file",
                            {
                                "path": path,
                                "offset": offset,
                                "limit": limit,
                                "show_line_numbers": show_line_numbers,
                            },
                            on_file_content=on_file_content,
                            on_file_chunk=on_file_chunk,
                            on_file_complete=on_file_complete,
                        )
                        if result["mode"] == "single" and result["content"] is not None:
                            raw_bytes = base64.b64decode(result["content"])
                        elif result["mode"] == "chunked":
                            if result["checksum"]:
                                md5 = hashlib.md5(result["chunks"]).hexdigest()
                                if md5 != result["checksum"]:
                                    raise Exception(
                                        f"Checksum mismatch: expected {result['checksum']}, got {md5}"
                                    )
                            raw_bytes = bytes(result["chunks"])
                        # Phantom-empty detection: agent acked the read but
                        # the content callback either fired with "" or never
                        # fired at all. Retry — the agent's view of the file
                        # is fine, our pipeline raced.
                        if raw_bytes is None or (raw_bytes == b"" and (limit > 0 or offset > 0)):
                            if serial_attempt < 2:
                                logger.info(
                                    f"Agent read of {path} returned empty (mode={result['mode']}, "
                                    f"attempt {serial_attempt + 1}/3); retrying"
                                )
                                last_attempt_was_empty = True
                                raw_bytes = None
                                time.sleep(0.05 * (serial_attempt + 1))
                                continue
                            # Out of retries — surface as failure so the
                            # caller (athena) doesn't render a phantom-empty
                            # markdown block. Better a visible error than
                            # silent corruption.
                            raise Exception(
                                f"Agent read of {path} returned empty after 3 attempts"
                            )
                        break
                    except Exception as exc:
                        agent_error = exc
                        logger.warning(
                            f"Agent read failed for {path}; trying debugfs fallback: {exc}"
                        )
                        break
                agent_total_lines = result.get("agent_total_lines")

            if raw_bytes is not None:
                agent_tl = agent_total_lines
                if show_header or show_footer or show_line_numbers:
                    return self._format_file_content(
                        raw_bytes, offset, limit, show_line_numbers,
                        show_header, show_footer, total_lines=agent_tl,
                    )
                return raw_bytes.decode("utf-8", errors="replace")

        if self._has_debugfs_rootfs():
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                temp_path = tmp.name
            try:
                try:
                    self._debugfs_download_file(path, temp_path)
                except Exception as dfs_err:
                    if agent_error is not None:
                        raise agent_error
                    raise dfs_err
                with open(temp_path, "rb") as f:
                    raw_bytes = f.read()
                if (
                    len(raw_bytes) >= _DEBUGFS_FULL_FILE_FALLBACK_LOG_THRESHOLD
                    and (offset > 0 or limit > 0 or show_line_numbers)
                ):
                    logger.warning(
                        "debugfs fallback read %s bytes from %s for host-side "
                        "file formatting",
                        len(raw_bytes),
                        path,
                    )
                return self._format_file_content(
                    raw_bytes, offset, limit, show_line_numbers, show_header, show_footer
                )
            finally:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

        if agent_error is not None:
            raise agent_error
        raise Exception(
            f"Failed to read {path}: agent unavailable and debugfs fallback unavailable"
        )

    @staticmethod
    def _format_file_content(
        raw_bytes: bytes,
        offset: int = 0,
        limit: int = 0,
        show_line_numbers: bool = False,
        show_header: bool = True,
        show_footer: bool = True,
        total_lines: int = None,
    ) -> str:
        """Apply offset, limit, line numbers, header, and footer to raw file content.

        When *total_lines* is provided, the content is assumed to be pre-sliced
        by the agent (offset/limit already applied) and total_lines is used for
        the header/footer decoration instead of computing it locally.  This
        avoids pulling the entire file over vsock/serial just for the line
        count.
        """
        text = raw_bytes.decode("utf-8", errors="replace")
        lines = text.split("\n")

        if total_lines is not None:
            # Agent already sliced; lines is just the requested window.
            selected = lines
            _total = total_lines
            _start = offset
        else:
            # Legacy path: full file content, slice locally.
            _total = len(lines)
            _start = min(offset, _total)
            _end = _total if limit == 0 else min(_start + limit, _total)
            selected = lines[_start:_end]

        _end = _start + len(selected)
        result_lines = []

        # Header: skipped lines indicator
        if show_header and _start > 0:
            result_lines.append(f"... skipped {_start} lines")

        # Line numbers
        if show_line_numbers:
            for i, line in enumerate(selected, start=_start + 1):
                result_lines.append(f"{i}\t{line}")
        else:
            result_lines.extend(selected)

        # Footer: remaining lines indicator
        if show_footer and _end < _total:
            remaining = _total - _end
            result_lines.append(f"... {remaining} lines left")

        return "\n".join(result_lines)

    def list_dir(self, path: str) -> list:
        """Lists directory contents.

        Uses the fast-read RPC when available (athena → runner) to bypass
        the slow serial console for the JSON listing. Falls back to the
        legacy serial path if the fastread socket isn't reachable.
        """
        if not self.agent_ready:
            raise Exception("Agent not ready")

        # FastRead RPC list_dir variant — same socket, op="list_dir"
        if self.vsock_listener is None:
            fr_path = self._fastread_socket_path_for_remote()
            if fr_path:
                deadline = time.time() + 1.5
                while not os.path.exists(fr_path) and time.time() < deadline:
                    time.sleep(0.025)
                if os.path.exists(fr_path):
                    try:
                        raw = self._fastread_remote(fr_path, path, op="list_dir")
                        if raw:
                            payload = json.loads(raw.decode("utf-8"))
                            return payload.get("files", [])
                    except Exception as exc:
                        logger.info(f"FastRead list_dir failed for {path}: {exc}")

        # Local-listener vsock fast path: send list_dir use_vsock=True and
        # wait on a registered buffer.
        if (
            self.vsock_enabled
            and self.vsock_listener is not None
            and getattr(self, "vsock_port", None)
        ):
            try:
                cmd_id = str(uuid.uuid4())
                slot = self.vsock_listener.register_pending_buffer(cmd_id)
                with self._event_callbacks_lock:
                    self.event_callbacks[cmd_id] = {
                        "on_stdout": None, "on_stderr": None,
                        "on_file_content": None, "on_file_chunk": None,
                        "on_file_complete": None, "on_dir_list": None,
                        "on_file_info": None, "on_status": None,
                        "on_exit": lambda code, _s=slot: code != 0 and _s["done"].set(),
                        "on_error": lambda msg, _s=slot: _s["done"].set(),
                    }
                self._write_to_agent(json.dumps({
                    "id": cmd_id, "type": "list_dir",
                    "path": path, "use_vsock": True, "vsock_port": self.vsock_port,
                }) + "\n")
                if slot["done"].wait(timeout=15) and slot["buf"] and not slot["error"]:
                    payload = json.loads(bytes(slot["buf"]).decode("utf-8"))
                    return payload.get("files", [])
            except Exception as exc:
                logger.info(f"Local vsock list_dir failed for {path}: {exc}")
            finally:
                try:
                    self.vsock_listener.unregister_pending_buffer(cmd_id)
                except Exception:
                    pass

        result = {}

        def on_dir_list(files):
            result["files"] = files

        self.send_request("list_dir", {"path": path}, on_dir_list=on_dir_list)
        return result.get("files", [])

    def download_file(self, remote_path: str, local_path: str, timeout: int = 300):
        """Downloads a file from the VM to the local filesystem.

        Prefers the vsock fast path when the listener is running: we register
        a pending upload under a pre-allocated cmd_id, send ``read_file`` with
        that id, and the guest uploads the file directly to the listener which
        writes it to ``local_path`` at native speed. If vsock is unavailable
        the agent automatically falls back to chunked serial (file_chunk
        events) which we handle below.

        Args:
            remote_path: Path to file in VM
            local_path: Path to save file locally
            timeout: Timeout in seconds (default 300 for large files over serial)
        """
        if not self.agent_ready:
            if self._has_debugfs_rootfs():
                self._debugfs_download_file(remote_path, local_path)
                return
            raise Exception("Agent not ready")

        import base64
        import hashlib

        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        cmd_id = str(uuid.uuid4())

        # Pre-register the upload path with the listener so the guest's vsock
        # connection can be routed to the right destination file even though
        # the request itself travels over serial.
        if self.vsock_enabled and self.vsock_listener is not None:
            self.vsock_listener.register_pending_upload(cmd_id, local_path)

        result = {
            "mode": None,
            "content": None,
            "file_handle": None,
            "md5": None,
            "error": None,
            "vsock_success": False,
        }

        def on_file_content(content):
            """Handle small file (single shot transfer over serial)."""
            result["mode"] = "single"
            result["content"] = content

        def on_file_chunk(data, offset, size):
            """Handle file chunk (streaming transfer over serial)."""
            if result["mode"] is None:
                result["mode"] = "chunked"
                result["file_handle"] = open(local_path, "wb")
                result["md5"] = hashlib.md5()

            decoded = base64.b64decode(data)
            result["file_handle"].write(decoded)
            result["md5"].update(decoded)

        def on_file_complete(total_size, checksum):
            """Handle file transfer completion (serial chunked path)."""
            if result["file_handle"]:
                result["file_handle"].close()
                result["file_handle"] = None
            result["checksum"] = checksum
            result["total_size"] = total_size

        def on_status(payload):
            """Pick up the vsock fast-path completion signal."""
            if payload.get("status") == "uploaded":
                result["vsock_success"] = True
                result["mode"] = "vsock"

        agent_error = None
        try:
            payload = {"path": remote_path, "use_vsock": bool(self.vsock_enabled)}
            if self.vsock_port:
                payload["vsock_port"] = self.vsock_port
            self._send_request_with_id(
                cmd_id,
                "read_file",
                payload,
                on_file_content=on_file_content,
                on_file_chunk=on_file_chunk,
                on_file_complete=on_file_complete,
                on_status=on_status,
                timeout=timeout,
            )

            if result["mode"] == "vsock" and result["vsock_success"]:
                if not os.path.exists(local_path):
                    raise Exception(
                        f"Vsock transfer reported success but file not found: {local_path}"
                    )
                return

            if result["mode"] == "single" and result["content"] is not None:
                data = base64.b64decode(result["content"])
                with open(local_path, "wb") as f:
                    f.write(data)
                return

            if result["mode"] == "chunked":
                if result.get("checksum") and result.get("md5"):
                    local_checksum = result["md5"].hexdigest()
                    if local_checksum != result["checksum"]:
                        raise Exception(
                            f"Checksum mismatch: expected {result['checksum']}, got {local_checksum}"
                        )
                return

            raise Exception(f"Failed to download {remote_path} via agent")
        except Exception as exc:
            agent_error = exc

        finally:
            if result.get("file_handle"):
                result["file_handle"].close()
            # Drop any registration we made so the listener doesn't hold
            # a reference to local_path beyond this call.
            if self.vsock_enabled and self.vsock_listener is not None:
                self.vsock_listener.unregister_pending_upload(cmd_id)

        if self._has_debugfs_rootfs():
            logger.warning(
                f"Agent download path failed for {remote_path}; trying debugfs fallback: {agent_error}"
            )
            self._debugfs_download_file(remote_path, local_path)
            return

        raise Exception(
            f"Failed to download {remote_path} via agent and debugfs fallback unavailable: {agent_error}"
        )

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        timeout: int = None,
        append: bool = False,
    ):
        """Uploads a file from local filesystem to the VM.

        Uses the vsock fast path when available (guest downloads directly from
        listener), falling back to chunked serial uploads.

        Args:
            local_path: Path to local file
            remote_path: Path in VM to write to
            timeout: Optional timeout in seconds (default: scales with file size)
            append: If True, append to remote_path instead of overwriting it.
        """
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        if not self.agent_ready:
            raise Exception("Agent not ready")

        with open(local_path, "rb") as f:
            content = f.read()

        self._write_bytes(remote_path, content, timeout=timeout, append=append)

    def write_text(
        self,
        remote_path: str,
        content: str,
        timeout: int = None,
        append: bool = False,
    ):
        """Write UTF-8 text directly to a file in the VM.

        Small and medium text writes go as one JSON request without creating a
        host temp file. Larger payloads use the raw bytes path so vsock can take
        over when available, with serial chunking as the compatibility fallback.
        """
        if not self.agent_ready:
            raise Exception("Agent not ready")

        encoded = (content or "").encode("utf-8")
        if len(encoded) <= _DIRECT_TEXT_WRITE_MAX_BYTES:
            if timeout is None:
                timeout = 30
            self.send_request(
                "write_text",
                {"path": remote_path, "content": content or "", "append": append},
                timeout=timeout,
            )
            return

        self._write_bytes(remote_path, encoded, timeout=timeout, append=append)

    def append_text(self, remote_path: str, content: str, timeout: int = None):
        """Append UTF-8 text directly to a file in the VM."""
        self.write_text(remote_path, content, timeout=timeout, append=True)

    def write_bytes(
        self,
        remote_path: str,
        content: bytes,
        timeout: int = None,
        append: bool = False,
    ):
        """Write bytes to a file in the VM without requiring a local temp file."""
        self._write_bytes(remote_path, bytes(content), timeout=timeout, append=append)

    def _write_bytes(
        self,
        remote_path: str,
        content: bytes,
        timeout: int = None,
        append: bool = False,
    ):
        if not self.agent_ready:
            raise Exception("Agent not ready")

        file_size = len(content)

        # Calculate timeout based on file size: minimum 30s, +10s per MB
        if timeout is None:
            file_size_mb = file_size / (1024 * 1024)
            timeout = max(30, int(30 + file_size_mb * 10))

        # FastWrite RPC: when we don't own a local vsock listener (e.g.
        # athena's webui talking to a detached runner) ask the runner to
        # do the write via its in-namespace listener. This unlocks vsock
        # speed for ManagedMicroVM uploads — without it large writes go
        # through the serial chunked path and contend with reads.
        if (
            getattr(self, "vsock_listener", None) is None
            and file_size > 512
        ):
            fw_path = self._fastwrite_socket_path_for_remote()
            if fw_path:
                deadline = time.time() + 1.5
                while not os.path.exists(fw_path) and time.time() < deadline:
                    time.sleep(0.025)
                if os.path.exists(fw_path):
                    try:
                        self._fastwrite_remote(
                            fw_path, remote_path, content, append=append, timeout=timeout
                        )
                        return
                    except Exception as exc:
                        logger.info(
                            f"FastWrite RPC failed for {remote_path}: {exc}"
                        )

        # Vsock fast path: register content for download, tell guest to fetch it.
        if (
            getattr(self, "vsock_enabled", False)
            and getattr(self, "vsock_listener", None) is not None
            and file_size > 512  # Small files are faster via serial
        ):
            cmd_id = str(uuid.uuid4())
            self.vsock_listener.register_pending_download(cmd_id, content)
            try:
                self._send_request_with_id(
                    cmd_id,
                    "write_file_vsock",
                    {
                        "path": remote_path,
                        "vsock_port": self.vsock_port,
                        "append": append,
                    },
                    timeout=timeout,
                )
                return
            except Exception:
                logger.debug(
                    f"Vsock write_file failed, falling back to serial for {remote_path}"
                )
            finally:
                self.vsock_listener.unregister_pending_download(cmd_id)

        import base64

        CHUNK_SIZE = _SERIAL_WRITE_CHUNK_SIZE

        if file_size <= CHUNK_SIZE:
            encoded = base64.b64encode(content).decode("utf-8")
            self.send_request(
                "write_file",
                {"path": remote_path, "content": encoded, "append": append},
                timeout=timeout,
            )
            return

        first_chunk = content[:CHUNK_SIZE]
        encoded = base64.b64encode(first_chunk).decode("utf-8")
        self.send_request(
            "write_file",
            {"path": remote_path, "content": encoded, "append": append},
            timeout=timeout,
        )

        offset = CHUNK_SIZE
        while offset < file_size:
            chunk = content[offset : offset + CHUNK_SIZE]
            encoded = base64.b64encode(chunk).decode("utf-8")
            self.send_request(
                "write_file",
                {"path": remote_path, "content": encoded, "append": True},
                timeout=timeout,
            )
            offset += CHUNK_SIZE

    def upload_folder(
        self,
        local_path: str,
        remote_path: str,
        pattern: str = None,
        skip_pattern: list[str] = None,
    ):
        """
        Uploads a folder recursively using agent file operations.
        """
        import fnmatch
        from pathlib import Path

        local_path = Path(local_path)
        if not local_path.is_dir():
            raise NotADirectoryError(f"Local path is not a directory: {local_path}")

        if not self.agent_ready:
            raise Exception("Agent not ready")

        for root, dirs, files in os.walk(local_path):
            rel_root = Path(root).relative_to(local_path)
            remote_root = Path(remote_path) / rel_root

            if skip_pattern:
                for d in list(dirs):
                    if any(fnmatch.fnmatch(d, sp) for sp in skip_pattern):
                        dirs.remove(d)

            for d in dirs:
                r_dir = remote_root / d
                logger.debug(f"Creating remote dir: {r_dir}")
                self.send_request(
                    "exec",
                    {
                        "command": f"mkdir -p {r_dir}",
                        "background": False,
                        "env": self.env_vars,
                    },
                )

            for file in files:
                if pattern and not fnmatch.fnmatch(file, pattern):
                    continue
                if skip_pattern and any(
                    fnmatch.fnmatch(file, sp) for sp in skip_pattern
                ):
                    continue

                local_file_path = str(Path(root) / file)
                remote_file_path = str(remote_root / file)

                logger.debug(f"Uploading {local_file_path} to {remote_file_path}")
                self.upload_file(local_file_path, remote_file_path)

    def get_file_info(self, path: str) -> dict:
        """Gets file information (size, mtime, etc.) from the VM."""
        if not self.agent_ready:
            raise Exception("Agent not ready")

        result = {}

        def on_file_info(info):
            result["info"] = info

        self.send_request("file_info", {"path": path}, on_file_info=on_file_info)
        return result.get("info", {})
