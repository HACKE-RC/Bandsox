"""
Vsock Host Listener

Listens for guest-initiated vsock connections. According to Firecracker docs,
when a guest connects via AF_VSOCK to a port, Firecracker forwards the connection
to a Unix socket at `{uds_path}_{port}`.

This module creates and manages those listener sockets.
"""

import base64
import hashlib
import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

from .protocol import (
    CHUNK_SIZE,
    RequestType,
    ResponseType,
    UploadRequest,
    DownloadRequest,
    encode_message,
    parse_request,
)

logger = logging.getLogger(__name__)


class VsockHostListener:
    """Listens for guest-initiated vsock connections.

    Firecracker routes guest AF_VSOCK connections to Unix sockets at:
    {uds_path}_{port}

    For example, if uds_path="/tmp/bandsox/vsock_vm123.sock" and port=9000,
    the listener socket will be at "/tmp/bandsox/vsock_vm123.sock_9000"

    When the guest connects via socket.connect((2, 9000)), Firecracker
    forwards to our listener socket.
    """

    def __init__(
        self,
        uds_path: str,
        port: int,
        on_upload: Optional[Callable[[str, bytes, str], bool]] = None,
        on_download: Optional[Callable[[str], Optional[bytes]]] = None,
    ):
        """Initialize the vsock host listener.

        Args:
            uds_path: Base path for the Firecracker vsock device (e.g., /tmp/bandsox/vsock_vm.sock)
            port: Port number to listen on (will create socket at uds_path_port)
            on_upload: Callback for uploads (path, data, checksum) -> success
            on_download: Callback for downloads (path) -> data or None
        """
        self.uds_path = uds_path
        self.port = port
        self.listener_path = f"{uds_path}_{port}"

        self.on_upload = on_upload
        self.on_download = on_download

        self.listener_socket: Optional[socket.socket] = None
        self.accept_thread: Optional[threading.Thread] = None
        self.running = False
        self._lock = threading.Lock()

        # Pending uploads: maps cmd_id -> local_path for download_file operations
        # When VM.download_file() is called, it registers the expected upload here
        # so we know where to write the file when the guest sends it
        self._pending_uploads: dict[str, str] = {}
        self._pending_uploads_lock = threading.Lock()

    def start(self):
        """Start listening for guest connections."""
        with self._lock:
            if self.running:
                logger.warning(
                    f"VsockHostListener already running on {self.listener_path}"
                )
                return

            # Clean up any stale socket
            if os.path.exists(self.listener_path):
                try:
                    os.unlink(self.listener_path)
                    logger.debug(f"Removed stale socket: {self.listener_path}")
                except Exception as e:
                    logger.warning(
                        f"Failed to remove stale socket {self.listener_path}: {e}"
                    )

            # Ensure parent directory exists
            Path(self.listener_path).parent.mkdir(parents=True, exist_ok=True)

            # Create and bind listener socket
            self.listener_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                self.listener_socket.bind(self.listener_path)
                self.listener_socket.listen(5)
                self.listener_socket.settimeout(
                    1.0
                )  # Allow periodic checks for shutdown

                self.running = True
                self.accept_thread = threading.Thread(
                    target=self._accept_loop,
                    daemon=True,
                    name=f"vsock-listener-{self.port}",
                )
                self.accept_thread.start()

                logger.info(f"VsockHostListener started on {self.listener_path}")

            except Exception as e:
                logger.error(f"Failed to start VsockHostListener: {e}")
                if self.listener_socket:
                    self.listener_socket.close()
                    self.listener_socket = None
                raise

    def stop(self):
        """Stop the listener and clean up resources."""
        with self._lock:
            if not self.running:
                return

            self.running = False

        # Close listener socket to interrupt accept()
        if self.listener_socket:
            try:
                self.listener_socket.close()
            except Exception as e:
                logger.debug(f"Error closing listener socket: {e}")
            self.listener_socket = None

        # Wait for accept thread to finish
        if self.accept_thread and self.accept_thread.is_alive():
            self.accept_thread.join(timeout=2)
            self.accept_thread = None

        # Remove socket file
        if os.path.exists(self.listener_path):
            try:
                os.unlink(self.listener_path)
                logger.debug(f"Removed listener socket: {self.listener_path}")
            except Exception as e:
                logger.warning(f"Failed to remove socket {self.listener_path}: {e}")

        logger.info(f"VsockHostListener stopped on {self.listener_path}")

    def register_pending_upload(self, cmd_id: str, local_path: str):
        """Register an expected upload from the guest.

        When VM.download_file() is called, it registers the expected upload here
        before sending the read_file request to the guest. This way, when the
        guest connects via vsock to upload the file, we know where to write it.

        Args:
            cmd_id: The command ID that will be sent with the upload request
            local_path: Where to write the file on the host
        """
        with self._pending_uploads_lock:
            self._pending_uploads[cmd_id] = local_path
            logger.debug(
                f"Registered pending upload: cmd_id={cmd_id}, path={local_path}"
            )

    def unregister_pending_upload(self, cmd_id: str):
        """Unregister a pending upload (e.g., on timeout or cancellation)."""
        with self._pending_uploads_lock:
            if cmd_id in self._pending_uploads:
                del self._pending_uploads[cmd_id]
                logger.debug(f"Unregistered pending upload: cmd_id={cmd_id}")

    def get_pending_upload_path(self, cmd_id: str) -> Optional[str]:
        """Get the local path for a pending upload, if registered."""
        with self._pending_uploads_lock:
            return self._pending_uploads.get(cmd_id)

    def _accept_loop(self):
        """Accept incoming connections and spawn handler threads."""
        logger.debug(f"Accept loop started for {self.listener_path}")

        while self.running:
            try:
                client_socket, _ = self.listener_socket.accept()
                client_socket.settimeout(30)  # 30s timeout for operations

                # Spawn handler thread for this connection
                handler = threading.Thread(
                    target=self._handle_connection,
                    args=(client_socket,),
                    daemon=True,
                    name=f"vsock-handler-{self.port}",
                )
                handler.start()

            except socket.timeout:
                # Normal timeout, check if still running
                continue
            except OSError as e:
                if self.running:
                    logger.error(f"Accept error: {e}")
                break
            except Exception as e:
                if self.running:
                    logger.error(f"Unexpected accept error: {e}")
                break

        logger.debug(f"Accept loop ended for {self.listener_path}")

    def _handle_connection(self, client: socket.socket):
        """Handle a single guest connection.

        Protocol:
        1. Read JSON request (newline-delimited)
        2. Process based on request type
        3. Send response(s)
        4. Close connection
        """
        try:
            # Read the request line
            buffer = b""
            while b"\n" not in buffer:
                chunk = client.recv(4096)
                if not chunk:
                    logger.debug("Client disconnected before sending request")
                    return
                buffer += chunk

            line, remaining = buffer.split(b"\n", 1)

            try:
                data = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON from guest: {e}")
                self._send_error(client, "unknown", f"Invalid JSON: {e}")
                return

            request = parse_request(data)
            if request is None:
                logger.error(f"Unknown request type: {data.get('type')}")
                self._send_error(
                    client, data.get("cmd_id", "unknown"), "Unknown request type"
                )
                return

            # Handle ping
            if isinstance(request, dict) and request.get("type") == "ping":
                self._send_message(
                    client,
                    {
                        "type": ResponseType.PONG.value,
                        "cmd_id": request.get("cmd_id", "ping"),
                    },
                )
                return

            # Handle upload (guest -> host)
            if isinstance(request, UploadRequest):
                self._handle_upload(client, request, remaining)
                return

            # Handle download (host -> guest)
            if isinstance(request, DownloadRequest):
                self._handle_download(client, request)
                return

            logger.error(f"Unhandled request type: {type(request)}")
            self._send_error(client, "unknown", "Unhandled request type")

        except socket.timeout:
            logger.warning("Client connection timed out")
        except Exception as e:
            logger.error(f"Error handling connection: {e}", exc_info=True)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _handle_upload(
        self, client: socket.socket, request: UploadRequest, initial_data: bytes
    ):
        """Handle file upload from guest to host.

        Protocol:
        1. Guest sends UploadRequest with path, size, checksum, cmd_id
        2. Host sends ReadyResponse
        3. Guest sends raw binary data (size bytes)
        4. Host verifies checksum and sends CompleteResponse or ErrorResponse

        The destination path is determined by:
        1. If cmd_id is registered via register_pending_upload(), use that path
        2. Else if on_upload callback is set, call it with the data
        3. Else write to request.path (legacy behavior, not recommended)
        """
        # Determine destination path - check pending uploads first
        dest_path = self.get_pending_upload_path(request.cmd_id)
        if dest_path:
            logger.info(
                f"Handling upload: {request.path} -> {dest_path} ({request.size} bytes)"
            )
        else:
            logger.info(f"Handling upload: {request.path} ({request.size} bytes)")

        # Send ready response
        self._send_message(
            client,
            {
                "type": ResponseType.READY.value,
                "cmd_id": request.cmd_id,
            },
        )

        # Receive file data
        received = len(initial_data)
        md5 = hashlib.md5()
        md5.update(initial_data)
        chunks = [initial_data] if initial_data else []

        while received < request.size:
            try:
                chunk = client.recv(min(CHUNK_SIZE, request.size - received))
                if not chunk:
                    self._send_error(
                        client, request.cmd_id, "Connection closed during upload"
                    )
                    return
                chunks.append(chunk)
                md5.update(chunk)
                received += len(chunk)
            except socket.timeout:
                self._send_error(client, request.cmd_id, "Upload timed out")
                return

        # Verify checksum
        file_hash = md5.hexdigest()
        if file_hash != request.checksum:
            self._send_error(
                client,
                request.cmd_id,
                f"Checksum mismatch: expected {request.checksum}, got {file_hash}",
            )
            return

        # Write file
        data = b"".join(chunks)

        try:
            if dest_path:
                # Write to registered destination path (from download_file)
                Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as f:
                    f.write(data)
                # Unregister after successful write
                self.unregister_pending_upload(request.cmd_id)
                final_path = dest_path
            elif self.on_upload:
                # Use callback
                success = self.on_upload(request.path, data, request.checksum)
                if not success:
                    self._send_error(client, request.cmd_id, "Upload callback failed")
                    return
                final_path = request.path
            else:
                # Default: write to request.path (legacy, not recommended)
                logger.warning(
                    f"Upload with no registered path or callback - writing to {request.path}"
                )
                Path(request.path).parent.mkdir(parents=True, exist_ok=True)
                with open(request.path, "wb") as f:
                    f.write(data)
                final_path = request.path
        except Exception as e:
            self._send_error(client, request.cmd_id, f"Failed to write file: {e}")
            return

        # Send success
        self._send_message(
            client,
            {
                "type": ResponseType.COMPLETE.value,
                "cmd_id": request.cmd_id,
                "size": received,
            },
        )

        logger.info(f"Upload complete: {final_path} ({received} bytes)")

    def _handle_download(self, client: socket.socket, request: DownloadRequest):
        """Handle file download from host to guest.

        Protocol:
        1. Guest sends DownloadRequest with path
        2. Host sends ChunkResponse messages with base64-encoded data
        3. Host sends CompleteResponse with checksum
        """
        logger.info(f"Handling download: {request.path}")

        # Get file data
        if self.on_download:
            try:
                data = self.on_download(request.path)
                if data is None:
                    self._send_error(
                        client, request.cmd_id, f"File not found: {request.path}"
                    )
                    return
            except Exception as e:
                self._send_error(client, request.cmd_id, f"Download error: {e}")
                return
        else:
            # Default: read from path
            if not os.path.exists(request.path):
                self._send_error(
                    client, request.cmd_id, f"File not found: {request.path}"
                )
                return

            try:
                with open(request.path, "rb") as f:
                    data = f.read()
            except Exception as e:
                self._send_error(client, request.cmd_id, f"Failed to read file: {e}")
                return

        # Send file in chunks
        md5 = hashlib.md5()
        offset = 0

        while offset < len(data):
            chunk = data[offset : offset + CHUNK_SIZE]
            md5.update(chunk)

            encoded = base64.b64encode(chunk).decode("utf-8")

            self._send_message(
                client,
                {
                    "type": ResponseType.CHUNK.value,
                    "cmd_id": request.cmd_id,
                    "data": encoded,
                    "offset": offset,
                    "size": len(chunk),
                },
            )

            offset += len(chunk)

        # Send completion with checksum
        self._send_message(
            client,
            {
                "type": ResponseType.COMPLETE.value,
                "cmd_id": request.cmd_id,
                "size": len(data),
                "checksum": md5.hexdigest(),
            },
        )

        logger.info(f"Download complete: {request.path} ({len(data)} bytes)")

    def _send_message(self, client: socket.socket, msg: dict):
        """Send a JSON message to the client."""
        data = encode_message(msg)
        client.sendall(data)

    def _send_error(self, client: socket.socket, cmd_id: str, error: str):
        """Send an error response to the client."""
        logger.error(f"Vsock error [{cmd_id}]: {error}")
        self._send_message(
            client,
            {
                "type": ResponseType.ERROR.value,
                "cmd_id": cmd_id,
                "error": error,
            },
        )


class VsockListenerManager:
    """Manages multiple vsock listeners for different ports.

    Typically one listener per VM, but supports multiple ports if needed.
    """

    def __init__(self, uds_path: str):
        """Initialize the manager.

        Args:
            uds_path: Base path for vsock (e.g., /tmp/bandsox/vsock_vm123.sock)
        """
        self.uds_path = uds_path
        self.listeners: dict[int, VsockHostListener] = {}
        self._lock = threading.Lock()

    def add_listener(
        self,
        port: int,
        on_upload: Optional[Callable[[str, bytes, str], bool]] = None,
        on_download: Optional[Callable[[str], Optional[bytes]]] = None,
    ) -> VsockHostListener:
        """Add and start a listener on the given port.

        Args:
            port: Port number to listen on
            on_upload: Callback for uploads
            on_download: Callback for downloads

        Returns:
            The created listener
        """
        with self._lock:
            if port in self.listeners:
                raise ValueError(f"Listener already exists for port {port}")

            listener = VsockHostListener(
                self.uds_path,
                port,
                on_upload=on_upload,
                on_download=on_download,
            )
            listener.start()
            self.listeners[port] = listener
            return listener

    def remove_listener(self, port: int):
        """Stop and remove a listener."""
        with self._lock:
            if port in self.listeners:
                self.listeners[port].stop()
                del self.listeners[port]

    def stop_all(self):
        """Stop all listeners."""
        with self._lock:
            for listener in self.listeners.values():
                listener.stop()
            self.listeners.clear()

    def get_listener(self, port: int) -> Optional[VsockHostListener]:
        """Get a listener by port."""
        with self._lock:
            return self.listeners.get(port)
