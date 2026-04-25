"""
Vsock Protocol Definitions

This module defines the message protocol for guest-initiated vsock connections.
The guest connects to the host via AF_VSOCK and sends JSON requests.
The host responds with JSON messages.

Protocol Flow:
1. Guest connects to host via AF_VSOCK(CID=2, port)
2. Firecracker forwards connection to host's Unix socket at uds_path_PORT
3. Guest sends a JSON request (newline-delimited)
4. Host processes request and sends JSON responses (newline-delimited)
5. Connection closes after transfer completes

Message Format:
All messages are JSON objects followed by a newline character.
Binary data is base64-encoded within JSON for simplicity.
"""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any


class RequestType(str, Enum):
    """Request types sent from guest to host."""

    # File transfer requests
    UPLOAD = "upload"  # Guest wants to send file TO host
    DOWNLOAD = "download"  # Guest wants to receive file FROM host

    # Utility requests
    PING = "ping"  # Connection health check


class ResponseType(str, Enum):
    """Response types sent from host to guest."""

    READY = "ready"  # Host is ready to receive/send data
    CHUNK = "chunk"  # File data chunk
    COMPLETE = "complete"  # Transfer completed successfully
    ERROR = "error"  # Error occurred
    PONG = "pong"  # Response to ping


@dataclass
class UploadRequest:
    """Request from guest to upload a file to the host.

    The guest has a file it wants to send to the host.
    After host responds with READY, guest sends raw binary data.
    """

    path: str  # Destination path on host
    size: int  # File size in bytes
    checksum: str  # MD5 checksum for verification
    cmd_id: str  # Command ID for correlation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": RequestType.UPLOAD.value,
            "path": self.path,
            "size": self.size,
            "checksum": self.checksum,
            "cmd_id": self.cmd_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UploadRequest":
        return cls(
            path=data["path"],
            size=data["size"],
            checksum=data["checksum"],
            cmd_id=data["cmd_id"],
        )


@dataclass
class DownloadRequest:
    """Request from guest to download a file from the host.

    The guest wants to receive a file from the host.
    Host responds with CHUNK messages containing base64-encoded data.
    """

    path: str  # Source path on host
    cmd_id: str  # Command ID for correlation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": RequestType.DOWNLOAD.value,
            "path": self.path,
            "cmd_id": self.cmd_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DownloadRequest":
        return cls(
            path=data["path"],
            cmd_id=data["cmd_id"],
        )


@dataclass
class ReadyResponse:
    """Host is ready to receive upload data."""

    cmd_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": ResponseType.READY.value,
            "cmd_id": self.cmd_id,
        }


@dataclass
class ChunkResponse:
    """A chunk of file data (for downloads)."""

    cmd_id: str
    data: str  # Base64-encoded binary data
    offset: int  # Offset in file
    size: int  # Size of this chunk (decoded)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": ResponseType.CHUNK.value,
            "cmd_id": self.cmd_id,
            "data": self.data,
            "offset": self.offset,
            "size": self.size,
        }


@dataclass
class CompleteResponse:
    """Transfer completed successfully."""

    cmd_id: str
    size: int  # Total bytes transferred
    checksum: Optional[str] = None  # MD5 checksum (for downloads)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "type": ResponseType.COMPLETE.value,
            "cmd_id": self.cmd_id,
            "size": self.size,
        }
        if self.checksum:
            result["checksum"] = self.checksum
        return result


@dataclass
class ErrorResponse:
    """An error occurred."""

    cmd_id: str
    error: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": ResponseType.ERROR.value,
            "cmd_id": self.cmd_id,
            "error": self.error,
        }


# Constants
DEFAULT_PORT = 9000
CHUNK_SIZE = 64 * 1024  # 64KB chunks for vsock (much larger than serial)
HOST_CID = 2  # Well-known CID for host in vsock


def encode_message(msg: Dict[str, Any]) -> bytes:
    """Encode a message to bytes for transmission."""
    return json.dumps(msg).encode("utf-8") + b"\n"


def decode_message(data: bytes) -> Dict[str, Any]:
    """Decode a message from bytes."""
    return json.loads(data.decode("utf-8").strip())


def parse_request(data: Dict[str, Any]) -> Optional[Any]:
    """Parse a request from the guest.

    Returns the appropriate request object or None if invalid.
    """
    req_type = data.get("type")

    if req_type == RequestType.UPLOAD.value:
        return UploadRequest.from_dict(data)
    elif req_type == RequestType.DOWNLOAD.value:
        return DownloadRequest.from_dict(data)
    elif req_type == RequestType.PING.value:
        return {"type": "ping", "cmd_id": data.get("cmd_id", "ping")}

    return None
