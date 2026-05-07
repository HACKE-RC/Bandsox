"""Fast-read RPC server.

Bridges the runner process's local vsock listener to remote callers that
sit outside the runner's mount namespace (e.g. athena's webui using
ManagedMicroVM). Without this bridge, those callers can never engage the
vsock fast path because they cannot bind a listener on the in-namespace
UDS, so every read of a VM file falls back to the slow serial path and
parallel reads timeout.

Architecture:
    [athena] --AF_UNIX--> fastread.sock (runner) --register buffer + write
    read_file JSON to agent stdin--> [agent] --AF_VSOCK upload--> [vsock
    listener in runner] --bytes land in buffer--> [runner sends bytes
    back over fastread.sock] --[athena receives bytes]

Wire protocol on the fastread socket (length-prefixed binary):
    Request:   <4-byte BE length> <JSON: {"path": "...", "cmd_id": "..."}>
    Response:  one of
        <4-byte BE length=0xFFFFFFFF> <4-byte BE err_len> <utf-8 error msg>
        <4-byte BE length> <length bytes of file content>

A length of 0xFFFFFFFF (-1) signals an error frame.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_ERR_MARKER = 0xFFFFFFFF
_DEFAULT_MAX_BYTES = 256 * 1024 * 1024  # 256 MiB cap on a single read

# Error codes — kept stable so athena UI can match on them.
ERR_SATURATED = "saturated"        # try again with backoff
ERR_LISTENER_DOWN = "listener_down"  # vsock listener missing; transient
ERR_TIMEOUT = "timeout"            # operation didn't complete in time
ERR_NOT_FOUND = "not_found"        # path doesn't exist on guest
ERR_TOO_LARGE = "too_large"        # exceeded server-side size cap
ERR_AGENT_ERROR = "agent_error"    # guest agent reported a failure
ERR_INTERNAL = "internal"          # unexpected server-side issue
ERR_BAD_REQUEST = "bad_request"    # malformed client input


class FastReadServer:
    def __init__(
        self,
        socket_path: str,
        vsock_listener,
        write_to_agent: Callable[[str], None],
        vsock_port: int,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_concurrent: int = 256,
    ):
        self.socket_path = socket_path
        # Resolve the listener at handler time, not at construction time.
        # The runner's _supervise_listener restarts the listener (creating
        # a new VsockHostListener instance) when its accept loop dies; if
        # we kept the old reference here, every subsequent fastread would
        # register on a dead buffer dict that nobody reads from.
        if callable(vsock_listener):
            self._listener_getter = vsock_listener
        else:
            self._listener_getter = lambda _l=vsock_listener: _l
        self.write_to_agent = write_to_agent
        self.vsock_port = vsock_port
        self.max_bytes = max_bytes
        # Bound concurrent handlers so a burst of 10k connects can't exhaust
        # FDs or trigger OOM via a flood of in-memory buffers.
        self._handler_sem = threading.BoundedSemaphore(max_concurrent)
        self._server_sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._supervisor_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        if self._running:
            return
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except Exception:
            pass
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(self.socket_path)
        # Match the vsock listener backlog so a burst of athena requests
        # doesn't bounce off the fastread RPC accept queue and fall back
        # to serial.
        self._server_sock.listen(1024)
        # World-writable so unprivileged callers (athena) can connect.
        try:
            os.chmod(self.socket_path, 0o666)
        except Exception:
            pass
        self._server_sock.settimeout(1.0)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="fastread-accept"
        )
        self._accept_thread.start()
        # Supervisor restarts the accept loop if it ever dies. Without this
        # a fatal error in accept() leaves the socket bound but unanswered,
        # which would manifest as silent timeouts on athena.
        self._supervisor_thread = threading.Thread(
            target=self._supervise, daemon=True, name="fastread-supervisor"
        )
        self._supervisor_thread.start()
        logger.info(f"FastReadServer listening on {self.socket_path}")

    def stop(self):
        self._running = False
        try:
            if self._server_sock:
                self._server_sock.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except Exception:
            pass

    def _accept_loop(self):
        while self._running:
            try:
                client, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if not self._running:
                    return
                # EBADF / ENOTSOCK → listener gone; bail so supervisor rebinds
                if e.errno in (9, 22, 88):
                    logger.error(f"FastRead listener socket closed: {e}")
                    return
                # Transient (EMFILE/ENFILE/EINTR) — keep going
                logger.warning(f"Transient FastRead accept error: {e}")
                time.sleep(0.1)
                continue
            except Exception as e:
                if self._running:
                    logger.exception(f"Unexpected FastRead accept error: {e}")
                time.sleep(0.1)
                continue

            client.settimeout(120.0)
            # Bound concurrency by spawning the handler under a semaphore
            # acquired in BLOCKING mode inside a tiny dispatcher thread.
            # Failing fast on saturation pushed callers onto the slow
            # serial fallback under burst load, which then blew the 30s
            # request timeout — so block briefly here instead.
            t = threading.Thread(
                target=self._dispatch,
                args=(client,),
                daemon=True,
                name="fastread-dispatch",
            )
            t.start()

    def _dispatch(self, client: socket.socket):
        # Block-with-timeout so a wedged handler can't permanently lock
        # capacity. The 60s upper bound matches our per-request deadline;
        # if every slot is occupied longer than that, something is
        # genuinely broken and failing the surplus is the right move.
        if not self._handler_sem.acquire(timeout=60.0):
            try:
                self._send_err(client, "fastread server saturated; retry", code=ERR_SATURATED)
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
            return
        try:
            self._handle_client(client)
        finally:
            try:
                self._handler_sem.release()
            except Exception:
                pass

    def _supervise(self):
        """Restart the accept loop if it dies while we're still running."""
        while self._running:
            time.sleep(2)
            t = self._accept_thread
            if t is None or not t.is_alive():
                if not self._running:
                    return
                logger.warning("FastRead accept loop died; restarting")
                try:
                    self._accept_thread = threading.Thread(
                        target=self._accept_loop,
                        daemon=True,
                        name="fastread-accept",
                    )
                    self._accept_thread.start()
                except Exception:
                    logger.exception("Failed to restart FastRead accept loop")
                    time.sleep(5)

    def _handle_client(self, client: socket.socket):
        cmd_id = None
        try:
            req = self._recv_request(client)
            path = req.get("path")
            op = req.get("op", "read")  # "read" (default) or "list_dir"
            cmd_id = req.get("cmd_id") or str(uuid.uuid4())
            if not path:
                self._send_err(client, "missing path", code=ERR_BAD_REQUEST)
                return
            if op not in ("read", "list_dir"):
                self._send_err(client, f"unknown op {op!r}", code=ERR_BAD_REQUEST)
                return

            listener = self._listener_getter()
            if listener is None:
                self._send_err(client, "vsock listener unavailable", code=ERR_LISTENER_DOWN)
                return
            slot = listener.register_pending_buffer(
                cmd_id, max_bytes=self.max_bytes
            )
            try:
                # Tell the guest agent to upload via vsock. The listener
                # routes the upload to our registered buffer because the
                # cmd_ids match.
                payload = {
                    "id": cmd_id,
                    "type": "read_file" if op == "read" else "list_dir",
                    "path": path,
                    "use_vsock": True,
                    "vsock_port": self.vsock_port,
                }
                self.write_to_agent(json.dumps(payload) + "\n")

                # Wait for the upload to complete. 60s gives plenty of
                # margin for very large files; truly stuck reads are
                # caught on the caller side too.
                woke = slot["done"].wait(timeout=60.0)
                if not woke:
                    self._send_err(client, f"vsock read of {path} timed out", code=ERR_TIMEOUT)
                    return
                if slot["error"]:
                    # The agent reported a failure (most commonly a guest-side
                    # error like "file not found"). Tag accordingly so the UI
                    # can show a real error rather than "transient try again".
                    err_code = ERR_NOT_FOUND if "not found" in slot["error"].lower() else ERR_AGENT_ERROR
                    self._send_err(client, slot["error"], code=err_code)
                    return
                buf = bytes(slot["buf"])
                self._send_ok(client, buf)
            finally:
                try:
                    listener.unregister_pending_buffer(cmd_id)
                except Exception:
                    pass
        except (socket.timeout, ConnectionResetError, BrokenPipeError) as e:
            logger.warning(f"FastRead client closed mid-request: {e}")
        except Exception as e:
            logger.exception("FastRead handler crashed")
            try:
                self._send_err(client, f"handler error: {e}")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _recv_request(client: socket.socket) -> dict:
        hdr = b""
        while len(hdr) < 4:
            chunk = client.recv(4 - len(hdr))
            if not chunk:
                raise ConnectionResetError("client closed before header")
            hdr += chunk
        (length,) = struct.unpack(">I", hdr)
        if length == 0 or length > 1 << 20:  # sanity cap on request JSON
            raise ValueError(f"invalid request length: {length}")
        body = b""
        while len(body) < length:
            chunk = client.recv(length - len(body))
            if not chunk:
                raise ConnectionResetError("client closed mid-body")
            body += chunk
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _send_ok(client: socket.socket, data: bytes):
        client.sendall(struct.pack(">I", len(data)))
        if data:
            client.sendall(data)

    @staticmethod
    def _send_err(client: socket.socket, msg: str, code: str = ERR_INTERNAL):
        """Send a structured error frame.

        Wire format: <4-byte 0xFFFFFFFF> <4-byte body_len> <JSON {code, msg}>
        Backwards-compatibility note: previous versions sent the raw msg
        text in the body. New clients parse JSON; old clients see a
        non-UTF-8-clean string and treat it as the message — degrades
        gracefully.
        """
        body = json.dumps({"code": code, "msg": msg}).encode("utf-8")
        client.sendall(struct.pack(">I", _ERR_MARKER))
        client.sendall(struct.pack(">I", len(body)))
        if body:
            client.sendall(body)


def fastread_socket_path_for(socket_path: str, vm_id: str) -> str:
    """Return the canonical fastread UDS path for a VM id.

    Co-located with the Firecracker API socket so it's host-visible
    without the runner's mount-namespace bindings affecting it.
    """
    return str(Path(socket_path).parent / f"{vm_id}.fastread.sock")
