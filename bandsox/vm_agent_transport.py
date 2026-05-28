"""Agent transport over the serial console for MicroVM (mixin).

Split out of vm.py; recombined into MicroVM via inheritance. Owns the
console connection, the stdout read loop + JSON event dispatch, and the
request/response plumbing the exec and file-ops mixins call through `self`.
"""
import os
import json
import time
import uuid
import base64
import socket
import threading
import logging

logger = logging.getLogger(__name__)


class _AgentTransportMixin:
    def connect_to_console(self):
        """Connects to the console socket if not the owner."""
        if self.process:
            return  # We are owner, we use callbacks

        if not os.path.exists(self.console_socket_path):
            return  # Console socket not ready

        self.console_conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.console_conn.connect(self.console_socket_path)
        except (ConnectionRefusedError, FileNotFoundError):
            # This happens if the server restarted and the multiplexer is gone.
            # The VM process might still be running but we can't talk to it.
            logger.error(f"Failed to connect to console socket for {self.vm_id}")
            self.console_conn = None
            raise Exception("VM Agent connection lost. Please restart the VM.")

        # Start read thread
        t = threading.Thread(target=self._socket_read_loop, daemon=True)
        t.start()

        # Check if agent is ready (we might have missed the event)
        # Do NOT optimistically set ready. Use metadata check in wait_for_agent or send_request.
        # self.agent_ready = True  <-- REMOVED

    def _socket_read_loop(self):
        """Reads from console socket and parses events.

        On disconnect we clear console_conn so the next exec_command
        triggers a fresh connect_to_console() instead of writing to a
        dead socket (which would raise BrokenPipeError every time).

        We accumulate raw bytes and only decode whole lines. The previous
        implementation called bytes.decode('utf-8') on every recv() return,
        which silently raised whenever a multi-byte sequence (or, more
        commonly, kernel log noise containing odd chars) was split across
        the 4096-byte recv boundary. The exception broke the read loop
        and tore down the connection — but more insidiously, when wrapped
        differently it could cause partial drops that surfaced as
        checksum mismatches on chunked file reads.
        """
        buffer = b""
        try:
            while True:
                try:
                    data = self.console_conn.recv(65536)
                    if not data:
                        break
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            decoded = line.decode("utf-8") + "\n"
                        except UnicodeDecodeError:
                            decoded = line.decode("utf-8", errors="replace") + "\n"
                        self._handle_stdout_line(decoded)
                except Exception:
                    break
        finally:
            # Tear down so the next send_request reconnects.
            try:
                self.console_conn.close()
            except Exception:
                pass
            self.console_conn = None
            # Fail any in-flight callbacks so callers don't hang on a
            # completion_event that will never fire — without this, a
            # console drop wedges every concurrent read for the full
            # 30s/60s timeout and athena reports "timed out". The agent
            # may reconnect and resume the next request, but commands
            # already in flight cannot be recovered cleanly.
            try:
                with self._event_callbacks_lock:
                    pending = list(self.event_callbacks.items())
                    self.event_callbacks.clear()
                for cmd_id, cbs in pending:
                    on_error = cbs.get("on_error")
                    on_exit = cbs.get("on_exit")
                    try:
                        if on_error:
                            on_error("Console socket disconnected mid-request")
                    except Exception:
                        pass
                    try:
                        if on_exit:
                            on_exit(-1)
                    except Exception:
                        pass
            except Exception:
                pass
            logger.warning(
                f"Console socket read loop exited for {self.vm_id}; "
                "will reconnect on next request"
            )

    def _handle_stdout_line(self, line):
        """Parses a line from stdout (event).

        All event_callbacks accesses are protected by _event_callbacks_lock.
        For "exit" events we atomically pop the entry under the lock so that
        concurrent threads (retry path, vsock fast-path registration,
        _socket_read_loop teardown) can't race on the same cmd_id slot.
        Read-only callbacks are looked up under the lock and invoked outside
        it so a slow callback doesn't stall the dispatch thread.
        """
        import json

        try:
            event = json.loads(line)
            evt_type = event.get("type")
            payload = event.get("payload")

            if evt_type == "status":
                status = payload.get("status")
                cmd_id = payload.get("cmd_id")
                if status == "ready":
                    self.agent_ready = True
                    logger.info("Agent is ready")
                elif status == "started":
                    pid = payload.get("pid")
                    with self._event_callbacks_lock:
                        entry = self.event_callbacks.get(cmd_id)
                    if entry:
                        cb = entry.get("on_started")
                        if cb:
                            cb(pid)

                # Generic status dispatch — used by the vsock fast path
                # to notify the caller that the upload already landed on
                # disk (status == "uploaded") so download_file knows not
                # to wait for chunked serial events.
                if cmd_id:
                    with self._event_callbacks_lock:
                        entry = self.event_callbacks.get(cmd_id)
                    if entry:
                        cb = entry.get("on_status")
                        if cb:
                            try:
                                cb(payload)
                            except Exception:
                                pass

            elif evt_type == "output":
                cmd_id = payload.get("cmd_id")
                stream = payload.get("stream")
                data = payload.get("data")
                encoding = payload.get("encoding")
                if encoding == "base64" and isinstance(data, str):
                    try:
                        data = base64.b64decode(data).decode("utf-8", errors="replace")
                    except Exception:
                        pass
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get(f"on_{stream}")
                    if cb:
                        try:
                            cb(data)
                        except Exception:
                            pass  # Don't let callback crash the loop

            elif evt_type == "file_content":
                cmd_id = payload.get("cmd_id")
                content = payload.get("content")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                    if entry is not None:
                        total_lines = payload.get("total_lines")
                        if total_lines is not None:
                            entry["_agent_total_lines"] = total_lines
                        cb = entry.get("on_file_content")
                    else:
                        cb = None
                if cb:
                    cb(content)

            elif evt_type == "dir_list":
                cmd_id = payload.get("cmd_id")
                files = payload.get("files")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_dir_list")
                    if cb:
                        cb(files)

            elif evt_type == "file_info":
                cmd_id = payload.get("cmd_id")
                info = payload.get("info")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_file_info")
                    if cb:
                        cb(info)

            elif evt_type == "file_chunk":
                cmd_id = payload.get("cmd_id")
                data = payload.get("data")
                offset = payload.get("offset")
                size = payload.get("size")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_file_chunk")
                    if cb:
                        cb(data, offset, size)

            elif evt_type == "file_complete":
                cmd_id = payload.get("cmd_id")
                total_size = payload.get("total_size")
                checksum = payload.get("checksum")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                    if entry is not None:
                        total_lines = payload.get("total_lines")
                        if total_lines is not None:
                            entry["_agent_total_lines"] = total_lines
                        cb = entry.get("on_file_complete")
                    else:
                        cb = None
                if cb:
                    cb(total_size, checksum)

            elif evt_type == "exit":
                cmd_id = payload.get("cmd_id")
                exit_code = payload.get("exit_code")
                # Atomically take ownership so no other thread can touch
                # this cmd_id's entry after we remove it.
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.pop(cmd_id, None)
                if entry:
                    payload_cb = entry.get("on_exit_payload")
                    if payload_cb:
                        payload_cb(payload)
                    cb = entry.get("on_exit")
                    if cb:
                        cb(exit_code)

            elif evt_type == "error":
                cmd_id = payload.get("cmd_id")
                error = payload.get("error")
                logger.error(f"Agent error for cmd {cmd_id}: {error}")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_error")
                    if cb:
                        cb(error)

        except json.JSONDecodeError:
            # Log raw output that isn't JSON (kernel logs etc)
            logger.info(f"VM Output: {line.strip()}")
            pass

    def _read_loop(self):
        # Deprecated, logic moved to _handle_stdout_line and multiplexer
        pass

    def send_request(
        self,
        req_type: str,
        payload: dict,
        on_stdout=None,
        on_stderr=None,
        on_file_content=None,
        on_file_chunk=None,
        on_file_complete=None,
        on_dir_list=None,
        on_file_info=None,
        on_status=None,
        exit_metadata=None,
        timeout=30,
    ):
        """Sends a JSON request to the agent."""
        cmd_id = str(uuid.uuid4())
        return self._send_request_with_id(
            cmd_id,
            req_type,
            payload,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            on_file_content=on_file_content,
            on_file_chunk=on_file_chunk,
            on_file_complete=on_file_complete,
            on_dir_list=on_dir_list,
            on_file_info=on_file_info,
            on_status=on_status,
            exit_metadata=exit_metadata,
            timeout=timeout,
        )

    def _send_request_with_id(
        self,
        cmd_id: str,
        req_type: str,
        payload: dict,
        on_stdout=None,
        on_stderr=None,
        on_file_content=None,
        on_file_chunk=None,
        on_file_complete=None,
        on_dir_list=None,
        on_file_info=None,
        on_status=None,
        exit_metadata=None,
        timeout=30,
    ):
        """Send a request with a caller-supplied cmd_id.

        Callers that need to pre-register state with the vsock listener
        (e.g. download_file, which tells the listener where to write the
        incoming file) must know the cmd_id before the request is sent,
        so we expose this variant. send_request is the usual entry point
        and just generates a uuid for cmd_id.
        """
        if not self.agent_ready:
            if not self.process and not self.console_conn:
                self.connect_to_console()

            start = time.time()
            while not self.agent_ready:
                if time.time() - start > 10:
                    raise Exception("Agent not ready")
                time.sleep(0.1)

        payload["id"] = cmd_id
        payload["type"] = req_type

        completion_event = threading.Event()
        result = {"code": -1, "error": None}

        def on_exit_payload(payload):
            if exit_metadata is not None:
                exit_metadata.clear()
                exit_metadata.update(payload or {})

        def on_exit(code):
            result["code"] = code
            completion_event.set()

        def on_error(msg):
            result["error"] = msg

        with self._event_callbacks_lock:
            self.event_callbacks[cmd_id] = {
                "on_stdout": on_stdout,
                "on_stderr": on_stderr,
                "on_file_content": on_file_content,
                "on_file_chunk": on_file_chunk,
                "on_file_complete": on_file_complete,
                "on_dir_list": on_dir_list,
                "on_file_info": on_file_info,
                "on_status": on_status,
                "on_exit_payload": on_exit_payload,
                "on_exit": on_exit,
                "on_error": on_error,
            }

        req_str = json.dumps(payload)
        self._write_to_agent(req_str + "\n")

        def _drop_callback():
            with self._event_callbacks_lock:
                self.event_callbacks.pop(cmd_id, None)

        if not completion_event.wait(timeout):
            # Send a kill for this cmd_id so the in-VM child process is
            # actually stopped — otherwise it keeps producing output and
            # monopolises the serial console, wedging every subsequent
            # command on the same VM. Best-effort: a stale callback
            # entry is harmless (it'll be cleaned up when the kill's
            # exit event eventually arrives, or never if the agent is
            # already wedged — in which case the lane is unrecoverable
            # without a VM restart, which is fine because the wedge is
            # what we're preventing in the first place).
            try:
                kill_req = json.dumps({"id": cmd_id, "type": "kill"}) + "\n"
                self._write_to_agent(kill_req)
            except Exception as e:
                logger.debug(
                    "Failed to send kill for timed-out command %s: %s",
                    cmd_id,
                    e,
                    exc_info=True,
                )
            _drop_callback()
            raise TimeoutError("Command timed out")

        # Auto-reconnect-and-retry on transient console drops. Up to 3
        # extra attempts with brief backoff. Hides multiplexer blips and
        # GIL-pressure broadcast drops from athena.
        retry_attempts = 0
        max_retries = 3
        while (
            retry_attempts < max_retries
            and result["error"]
            and "console socket disconnected" in result["error"].lower()
        ):
            retry_attempts += 1
            logger.info(
                f"Console disconnect during cmd {cmd_id}; reconnect+retry "
                f"{retry_attempts}/{max_retries}"
            )
            # Force a fresh connection before retrying.
            try:
                if self.console_conn:
                    self.console_conn.close()
            except Exception:
                pass
            self.console_conn = None
            # Backoff a touch: gives the multiplexer a moment to drain
            # any backlog so we don't immediately get dropped again.
            time.sleep(0.05 * retry_attempts)
            # Reset state for the retry
            result["error"] = None
            result["code"] = -1
            completion_event.clear()
            with self._event_callbacks_lock:
                self.event_callbacks[cmd_id] = {
                    "on_stdout": on_stdout,
                    "on_stderr": on_stderr,
                    "on_file_content": on_file_content,
                    "on_file_chunk": on_file_chunk,
                    "on_file_complete": on_file_complete,
                    "on_dir_list": on_dir_list,
                    "on_file_info": on_file_info,
                    "on_status": on_status,
                    "on_exit_payload": on_exit_payload,
                    "on_exit": on_exit,
                    "on_error": on_error,
                }
            self._write_to_agent(req_str + "\n")
            if not completion_event.wait(timeout):
                try:
                    kill_req = json.dumps({"id": cmd_id, "type": "kill"}) + "\n"
                    self._write_to_agent(kill_req)
                except Exception as e:
                    logger.debug(
                        "Failed to send kill for timed-out command %s after %s "
                        "reconnect retries: %s",
                        cmd_id,
                        retry_attempts,
                        e,
                        exc_info=True,
                    )
                _drop_callback()
                raise TimeoutError(
                    f"Command timed out (after {retry_attempts} reconnect retries)"
                )

        if result["error"]:
            raise Exception(f"Agent error: {result['error']}")

        return result["code"]

    def _write_to_agent(self, data: str):
        """Writes data to the agent via multiplexer or socket.

        When the console socket is broken (e.g. the runner's multiplexer
        dropped us or the runner restarted), try to reconnect once before
        raising. A single BrokenPipeError without recovery would surface
        as "Error executing command: [Errno 32] Broken pipe" on every
        subsequent tool call, leaving the VM permanently wedged from
        the caller's perspective.
        """
        if self.multiplexer:
            with self._agent_write_lock:
                self.multiplexer.write_input(data)
            return

        payload = data.encode("utf-8")

        with self._agent_write_lock:
            if self.console_conn:
                try:
                    self.console_conn.sendall(payload)
                    return
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    logger.warning(
                        f"Console write failed ({e}); attempting reconnect"
                    )
                    try:
                        self.console_conn.close()
                    except Exception:
                        pass
                    self.console_conn = None

            # No connection or broken — try (re)connect.
            try:
                self.connect_to_console()
            except Exception as e:
                raise Exception(f"No connection to agent: {e}")

            if not self.console_conn:
                raise Exception("No connection to agent")

            self.console_conn.sendall(payload)
