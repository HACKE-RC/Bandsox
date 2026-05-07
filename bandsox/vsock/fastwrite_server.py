"""Fast-write RPC server.

Symmetric to fastread_server: bridges remote callers (athena's agent
tool runner using ManagedMicroVM) to the runner's local vsock listener
for the WRITE direction. Without this, vm.upload_file from a
ManagedMicroVM falls back to the slow serial chunked path which makes
large file writes painfully slow and parallel writes contend on the
serial console.

Flow:
    [athena] --AF_UNIX (header + bytes)--> fastwrite.sock (runner) -->
    register_pending_download(cmd_id, bytes) on local listener -->
    write `write_file_vsock` to agent stdin --> [agent] --AF_VSOCK
    download--> [listener serves bytes from registered download] -->
    [agent writes file] --emits exit event--> runner sees it via
    event_callbacks --> sends ok back over fastwrite.sock

Wire protocol (length-prefixed binary):
    Request:
        <4-byte BE json_len> <JSON header> <4-byte BE data_len> <data_len bytes>
        Header: {"op":"write", "path":"...", "cmd_id":"...", "append":bool}
        (size implied by data_len; cmd_id may be omitted)
    Response:
        <4-byte BE 0> on success (no payload)
        <4-byte BE 0xFFFFFFFF> <4-byte BE err_len> <utf-8 err msg> on error
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
_DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_MAX_HEADER_BYTES = 1 << 20

# Stable error codes — kept identical to fastread_server's set.
ERR_SATURATED = "saturated"
ERR_LISTENER_DOWN = "listener_down"
ERR_TIMEOUT = "timeout"
ERR_NOT_FOUND = "not_found"
ERR_TOO_LARGE = "too_large"
ERR_AGENT_ERROR = "agent_error"
ERR_INTERNAL = "internal"
ERR_BAD_REQUEST = "bad_request"


class FastWriteServer:
    def __init__(
        self,
        socket_path: str,
        vsock_listener,
        send_request_with_id: Callable,
        vsock_port: int,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_concurrent: int = 256,
    ):
        self.socket_path = socket_path
        if callable(vsock_listener):
            self._listener_getter = vsock_listener
        else:
            self._listener_getter = lambda _l=vsock_listener: _l
        # Function with the same signature as MicroVM._send_request_with_id
        # so we can dispatch a write_file_vsock request and wait for the
        # agent's exit event without re-implementing the callback dance.
        self._send_request_with_id = send_request_with_id
        self.vsock_port = vsock_port
        self.max_bytes = max_bytes
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
        self._server_sock.listen(1024)
        try:
            os.chmod(self.socket_path, 0o666)
        except Exception:
            pass
        self._server_sock.settimeout(1.0)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="fastwrite-accept"
        )
        self._accept_thread.start()
        self._supervisor_thread = threading.Thread(
            target=self._supervise, daemon=True, name="fastwrite-supervisor"
        )
        self._supervisor_thread.start()
        logger.info(f"FastWriteServer listening on {self.socket_path}")

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
                if e.errno in (9, 22, 88):
                    logger.error(f"FastWrite listener socket closed: {e}")
                    return
                logger.warning(f"Transient FastWrite accept error: {e}")
                time.sleep(0.1)
                continue
            except Exception as e:
                if self._running:
                    logger.exception(f"Unexpected FastWrite accept error: {e}")
                time.sleep(0.1)
                continue

            client.settimeout(120.0)
            t = threading.Thread(
                target=self._dispatch,
                args=(client,),
                daemon=True,
                name="fastwrite-dispatch",
            )
            t.start()

    def _supervise(self):
        while self._running:
            time.sleep(2)
            t = self._accept_thread
            if t is None or not t.is_alive():
                if not self._running:
                    return
                logger.warning("FastWrite accept loop died; restarting")
                try:
                    self._accept_thread = threading.Thread(
                        target=self._accept_loop,
                        daemon=True,
                        name="fastwrite-accept",
                    )
                    self._accept_thread.start()
                except Exception:
                    logger.exception("Failed to restart FastWrite accept loop")
                    time.sleep(5)

    def _dispatch(self, client: socket.socket):
        if not self._handler_sem.acquire(timeout=60.0):
            try:
                self._send_err(client, "fastwrite server saturated; retry", code=ERR_SATURATED)
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

    def _handle_client(self, client: socket.socket):
        # Per-phase timings, sampled cheaply. Logged only when the total
        # blows past a soft budget so we can root-cause the 200-concurrent
        # write bottleneck without noisy steady-state output.
        t_phase = {}
        t_phase["start"] = time.monotonic()
        try:
            header = self._recv_header(client)
            path = header.get("path")
            append = bool(header.get("append", False))
            cmd_id = header.get("cmd_id") or str(uuid.uuid4())
            t_phase["header"] = time.monotonic()
            if not path:
                self._send_err(client, "missing path", code=ERR_BAD_REQUEST)
                return

            data_len = self._recv_u32(client)
            if data_len > self.max_bytes:
                self._send_err(
                    client,
                    f"write {data_len} exceeds cap {self.max_bytes}",
                    code=ERR_TOO_LARGE,
                )
                return
            content = self._recvn(client, data_len)
            t_phase["recv_data"] = time.monotonic()

            listener = self._listener_getter()
            if listener is None:
                self._send_err(client, "vsock listener unavailable", code=ERR_LISTENER_DOWN)
                return
            listener.register_pending_download(cmd_id, content)
            t_phase["register"] = time.monotonic()
            try:
                self._send_request_with_id(
                    cmd_id,
                    "write_file_vsock",
                    {
                        "path": path,
                        "vsock_port": self.vsock_port,
                        "append": append,
                    },
                    timeout=60,
                )
                t_phase["agent_done"] = time.monotonic()
                self._send_ok(client)
                t_phase["respond"] = time.monotonic()
            except TimeoutError as e:
                self._send_err(client, str(e), code=ERR_TIMEOUT)
            except Exception as e:
                self._send_err(client, str(e), code=ERR_AGENT_ERROR)
            finally:
                try:
                    listener.unregister_pending_download(cmd_id)
                except Exception:
                    pass
                # Per-phase log only on slow paths so steady-state stays quiet.
                total = time.monotonic() - t_phase["start"]
                if total > 0.2:
                    last = t_phase["start"]
                    parts = []
                    for k in ("header", "recv_data", "register", "agent_done", "respond"):
                        if k in t_phase:
                            parts.append(f"{k}={(t_phase[k]-last)*1000:.0f}ms")
                            last = t_phase[k]
                    logger.info(
                        f"FastWrite slow path={path} size={data_len} total={total*1000:.0f}ms "
                        + " ".join(parts)
                    )
        except (socket.timeout, ConnectionResetError, BrokenPipeError) as e:
            logger.warning(f"FastWrite client closed mid-request: {e}")
        except Exception as e:
            logger.exception("FastWrite handler crashed")
            try:
                self._send_err(client, f"handler error: {e}")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    @classmethod
    def _recv_header(cls, client: socket.socket) -> dict:
        length = cls._recv_u32(client)
        if length == 0 or length > _MAX_HEADER_BYTES:
            raise ValueError(f"invalid header length: {length}")
        body = cls._recvn(client, length)
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _recv_u32(client: socket.socket) -> int:
        data = b""
        while len(data) < 4:
            chunk = client.recv(4 - len(data))
            if not chunk:
                raise ConnectionResetError("client closed before u32")
            data += chunk
        return struct.unpack(">I", data)[0]

    @staticmethod
    def _recvn(client: socket.socket, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = client.recv(min(65536, n - len(out)))
            if not chunk:
                raise ConnectionResetError("client closed mid-body")
            out.extend(chunk)
        return bytes(out)

    @staticmethod
    def _send_ok(client: socket.socket):
        client.sendall(struct.pack(">I", 0))

    @staticmethod
    def _send_err(client: socket.socket, msg: str, code: str = ERR_INTERNAL):
        body = json.dumps({"code": code, "msg": msg}).encode("utf-8")
        client.sendall(struct.pack(">I", _ERR_MARKER))
        client.sendall(struct.pack(">I", len(body)))
        if body:
            client.sendall(body)


def fastwrite_socket_path_for(socket_path: str, vm_id: str) -> str:
    return str(Path(socket_path).parent / f"{vm_id}.fastwrite.sock")
