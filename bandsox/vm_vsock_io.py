"""vsock bridge and fast read/write RPC paths for MicroVM (mixin).

Split out of vm.py; recombined into MicroVM via inheritance. Methods share
`self` and call transport/lifecycle siblings through the MRO. The .vsock
server imports stay lazy (inside methods), as in the original.
"""
import os
import json
import time
import shutil
import socket
import logging

from .vm_common import FastIOError, _parse_fastio_error

logger = logging.getLogger(__name__)


class _VsockIOMixin:
    def _setup_vsock_bridge(self, cid: int, port: int):
        """Configures Firecracker vsock and starts the host-side listener.

        Firecracker's vsock model:
          - Host-initiated connections: host connect(<uds_path>) + "CONNECT <port>\\n"
          - Guest-initiated connections: host listens on a Unix socket at
            "<uds_path>_<port>", Firecracker forwards guest AF_VSOCK(2, port)
            connections to that listener.

        The previous implementation called ``sock.connect(<uds_path>)`` and
        expected to receive guest-initiated traffic on it, which silently
        never worked — guests got ECONNRESET on every attempt and every
        read_file fell back to the serial console. We now run a proper
        VsockHostListener on ``<uds_path>_<port>`` instead.

        Args:
            cid: Guest Context ID (told to Firecracker via /vsock)
            port: Port the guest agent connects to for file transfers
        """
        from .vsock import VsockHostListener

        self.vsock_socket_path = f"/tmp/bandsox/vsock_{self.vm_id}.sock"
        self.vsock_baked_path = self.vsock_socket_path

        try:
            os.makedirs("/tmp/bandsox", exist_ok=True)
            os.chmod("/tmp/bandsox", 0o777)
        except PermissionError:
            pass

        # Pre-cleanup: Firecracker will create uds_path itself, so only
        # remove stale files. The listener socket (uds_path_port) is
        # ours — VsockHostListener.start() also unlinks it before binding.
        if os.path.exists(self.vsock_socket_path):
            try:
                os.unlink(self.vsock_socket_path)
                logger.debug(f"Removed stale vsock socket: {self.vsock_socket_path}")
            except Exception as e:
                logger.warning(
                    f"Failed to remove stale socket {self.vsock_socket_path}: {e}"
                )

        listener_path = f"{self.vsock_socket_path}_{port}"
        if os.path.exists(listener_path):
            try:
                os.unlink(listener_path)
            except Exception as e:
                logger.warning(
                    f"Failed to remove stale listener socket {listener_path}: {e}"
                )

        try:
            logger.debug(
                f"Configuring Firecracker vsock: CID={cid}, socket={self.vsock_socket_path}"
            )
            self.client.put_vsock("vsock0", cid, self.vsock_socket_path)

            # Firecracker creates <uds_path> asynchronously after the API call
            # returns. We wait up to 5s for it to appear so the listener has
            # a valid parent dir; the listener itself creates its own socket.
            max_wait = 50
            for _ in range(max_wait):
                if os.path.exists(self.vsock_socket_path):
                    break
                time.sleep(0.1)
            else:
                raise Exception(
                    f"Firecracker vsock socket not created at {self.vsock_socket_path}"
                )

            self.vsock_listener = VsockHostListener(
                uds_path=self.vsock_socket_path, port=port
            )
            self.vsock_listener.start()

            self.vsock_enabled = True
            self.vsock_cid = cid
            self.vsock_port = port

            self.env_vars["BANDSOX_VSOCK_PORT"] = str(port)
            logger.info(
                f"Vsock enabled: CID={cid}, port={port}, listener={listener_path}"
            )

            # Bring up the fast-read and fast-write RPC servers so
            # remote ManagedMicroVM callers (athena's webui process)
            # can route file ops through this listener instead of
            # falling back to chunked serial.
            try:
                self._start_fastread_server()
            except Exception as e:
                logger.warning(f"Failed to start fast-read RPC server: {e}")
            try:
                self._start_fastwrite_server()
            except Exception as e:
                logger.warning(f"Failed to start fast-write RPC server: {e}")

        except Exception as e:
            logger.error(f"Failed to setup vsock: {e}")
            self._cleanup_vsock_bridge()
            raise Exception(f"Failed to setup vsock: {e}") from e

    def setup_vsock_listener(self, port: int = None):
        """Start the host-side vsock listener for an already-running VM.

        Used after snapshot restore, where Firecracker has already
        recreated its vsock device from the snapshot and exposed
        ``<uds_path>`` again. We just need to re-attach our listener on
        ``<uds_path>_<port>`` so guest-initiated connections find a
        handler.
        """
        from .vsock import VsockHostListener

        if port is None:
            port = self.vsock_port
        if port is None:
            raise ValueError("No vsock port specified")
        if not self.vsock_socket_path:
            raise ValueError("No vsock socket path configured")

        # Stale listener socket will block bind() with EADDRINUSE; strip it.
        listener_path = f"{self.vsock_socket_path}_{port}"
        if os.path.exists(listener_path):
            try:
                os.unlink(listener_path)
            except Exception as e:
                logger.debug(f"Failed to remove stale listener {listener_path}: {e}")

        self.vsock_listener = VsockHostListener(
            uds_path=self.vsock_socket_path, port=port
        )
        self.vsock_listener.start()

        self.vsock_enabled = True
        self.vsock_port = port
        self.env_vars["BANDSOX_VSOCK_PORT"] = str(port)
        logger.info(
            f"Vsock listener started for restored VM: port={port}, path={listener_path}"
        )

        # Bring up the fast-read AND fast-write RPC servers alongside
        # the listener so any remote ManagedMicroVM (e.g. athena's webui
        # process, which lives outside the runner's mount namespace) can
        # route file ops through this listener instead of falling back
        # to the slow serial path.
        try:
            self._start_fastread_server()
        except Exception as e:
            logger.warning(f"Failed to start fast-read RPC server: {e}")
        try:
            self._start_fastwrite_server()
        except Exception as e:
            logger.warning(f"Failed to start fast-write RPC server: {e}")

    def _fastread_socket_path_for_remote(self) -> str:
        """Return the canonical fastread UDS path the runner would expose.

        Used by remote callers (ManagedMicroVM) that don't own a local
        listener but want to ask the runner to do a vsock read on their
        behalf.
        """
        if getattr(self, "fastread_socket_path", None):
            return self.fastread_socket_path
        try:
            from .vsock.fastread_server import fastread_socket_path_for
            return fastread_socket_path_for(self.socket_path, self.vm_id)
        except Exception:
            return None

    def _fastread_remote(self, sock_path: str, path: str, timeout: float = 60.0, op: str = "read") -> bytes:
        """Synchronous fast-read RPC client.

        Connects to the runner's fastread UDS, sends the request, returns
        the response bytes (file content for op="read", JSON-encoded
        listing for op="list_dir"). On error raises FastIOError with
        .code and .msg.
        """
        import struct as _struct
        import uuid as _uuid

        cmd_id = str(_uuid.uuid4())
        body = json.dumps({"path": path, "cmd_id": cmd_id, "op": op}).encode("utf-8")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(sock_path)
            sock.sendall(_struct.pack(">I", len(body)) + body)

            hdr = self._recvn(sock, 4)
            (length,) = _struct.unpack(">I", hdr)
            if length == 0xFFFFFFFF:
                err_hdr = self._recvn(sock, 4)
                (err_len,) = _struct.unpack(">I", err_hdr)
                err_body = self._recvn(sock, err_len) if err_len else b""
                raise _parse_fastio_error(err_body)
            if length == 0:
                return b""
            return self._recvn(sock, length)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _fastwrite_socket_path_for_remote(self) -> str:
        if getattr(self, "fastwrite_socket_path", None):
            return self.fastwrite_socket_path
        try:
            from .vsock.fastwrite_server import fastwrite_socket_path_for
            return fastwrite_socket_path_for(self.socket_path, self.vm_id)
        except Exception:
            return None

    def _fastwrite_remote(
        self,
        sock_path: str,
        remote_path: str,
        content: bytes,
        append: bool = False,
        timeout: float = 60.0,
    ):
        """Synchronous fast-write RPC client. Raises on error."""
        import struct as _struct
        import uuid as _uuid

        cmd_id = str(_uuid.uuid4())
        header = json.dumps(
            {"op": "write", "path": remote_path, "cmd_id": cmd_id, "append": bool(append)}
        ).encode("utf-8")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(sock_path)
            sock.sendall(_struct.pack(">I", len(header)) + header)
            sock.sendall(_struct.pack(">I", len(content)))
            if content:
                sock.sendall(content)

            hdr = self._recvn(sock, 4)
            (length,) = _struct.unpack(">I", hdr)
            if length == 0xFFFFFFFF:
                err_hdr = self._recvn(sock, 4)
                (err_len,) = _struct.unpack(">I", err_hdr)
                err_body = self._recvn(sock, err_len) if err_len else b""
                raise _parse_fastio_error(err_body)
            # length == 0 → success, no payload.
        finally:
            try:
                sock.close()
            except Exception:
                pass

    @staticmethod
    def _recvn(sock: socket.socket, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = sock.recv(min(65536, n - len(out)))
            if not chunk:
                raise ConnectionResetError("server closed mid-frame")
            out.extend(chunk)
        return bytes(out)

    def _start_fastread_server(self):
        """Start the fast-read RPC server for this VM.

        Idempotent — re-binds if already running. Safe to call from the
        runner (where vsock_listener is local) but a no-op on processes
        without a local listener.
        """
        from .vsock.fastread_server import (
            FastReadServer,
            fastread_socket_path_for,
        )

        if self.vsock_listener is None:
            return
        if getattr(self, "_fastread_server", None) is not None:
            return

        sock_path = fastread_socket_path_for(self.socket_path, self.vm_id)

        def _writer(data: str):
            self._write_to_agent(data)

        # Pass a getter rather than the listener instance so the server
        # tracks supervisor-driven listener restarts.
        srv = FastReadServer(
            socket_path=sock_path,
            vsock_listener=lambda: self.vsock_listener,
            write_to_agent=_writer,
            vsock_port=self.vsock_port,
        )
        srv.start()
        self._fastread_server = srv
        self.fastread_socket_path = sock_path

    def _start_fastwrite_server(self):
        from .vsock.fastwrite_server import (
            FastWriteServer,
            fastwrite_socket_path_for,
        )

        if self.vsock_listener is None:
            return
        if getattr(self, "_fastwrite_server", None) is not None:
            return

        sock_path = fastwrite_socket_path_for(self.socket_path, self.vm_id)
        srv = FastWriteServer(
            socket_path=sock_path,
            vsock_listener=lambda: self.vsock_listener,
            send_request_with_id=self._send_request_with_id,
            vsock_port=self.vsock_port,
        )
        srv.start()
        self._fastwrite_server = srv
        self.fastwrite_socket_path = sock_path

    def _cleanup_vsock_bridge(self):
        """Stop the vsock listener and release its socket.

        We do NOT delete ``vsock_socket_path`` — Firecracker owns that file
        and will unlink it when the VM exits. We only unlink the listener
        socket at ``<uds_path>_<port>`` inside VsockHostListener.stop().
        """
        logger.debug(f"Cleaning up vsock for {self.vm_id}")

        if self.vsock_listener is not None:
            try:
                self.vsock_listener.stop()
            except Exception as e:
                logger.debug(f"Error stopping vsock listener: {e}")
            self.vsock_listener = None

        # Legacy bridge cleanup — kept for any caller still writing to
        # these attributes from older code paths.
        self.vsock_bridge_running = False
        if self.vsock_bridge_socket is not None:
            try:
                self.vsock_bridge_socket.close()
            except Exception:
                pass
            self.vsock_bridge_socket = None
        if self.vsock_bridge_thread is not None and self.vsock_bridge_thread.is_alive():
            try:
                self.vsock_bridge_thread.join(timeout=1)
            except Exception:
                pass
            self.vsock_bridge_thread = None

        self.vsock_socket_path = None
        self.vsock_baked_path = None
        self.vsock_enabled = False
        self.vsock_cid = None
        self.vsock_port = None

        if "BANDSOX_VSOCK_PORT" in self.env_vars:
            del self.env_vars["BANDSOX_VSOCK_PORT"]

    def _cleanup_vsock_isolation(self):
        if not self.vsock_isolation_dir:
            return
        try:
            shutil.rmtree(self.vsock_isolation_dir)
            logger.debug(f"Removed vsock isolation dir: {self.vsock_isolation_dir}")
        except Exception as e:
            logger.warning(
                f"Failed to remove vsock isolation dir {self.vsock_isolation_dir}: {e}"
            )
        self.vsock_isolation_dir = None
