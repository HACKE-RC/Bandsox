"""
Bandsox Vsock Module

Provides high-speed file transfer between host and guest VMs using
Firecracker's virtio-vsock device with guest-initiated connections.

Architecture:
- Guest connects to host via AF_VSOCK socket (CID=2, port)
- Firecracker forwards connection to Unix socket at uds_path_PORT
- Host listener accepts connections and handles file transfer requests

Usage:
    from bandsox.vsock import VsockHostListener, DEFAULT_PORT

    # Create listener for a VM
    listener = VsockHostListener(
        uds_path="/var/lib/bandsox/vsock/vsock_vm123.sock",
        port=9000,
    )
    listener.start()

    # ... VM runs and initiates transfers ...

    listener.stop()
"""

from .protocol import (
    DEFAULT_PORT,
    CHUNK_SIZE,
    HOST_CID,
    RequestType,
    ResponseType,
    UploadRequest,
    DownloadRequest,
    encode_message,
    decode_message,
)

from .host_listener import (
    VsockHostListener,
    VsockListenerManager,
)

__all__ = [
    # Protocol constants
    "DEFAULT_PORT",
    "CHUNK_SIZE",
    "HOST_CID",
    # Protocol types
    "RequestType",
    "ResponseType",
    "UploadRequest",
    "DownloadRequest",
    # Protocol functions
    "encode_message",
    "decode_message",
    # Host-side classes
    "VsockHostListener",
    "VsockListenerManager",
]
