"""End-to-end tests for the guest-initiated vsock listener architecture.

These regression tests cover the failure mode that killed session
4c99a657 — the host-side code used to call ``sock.connect(uds_path)``
and expect to *receive* guest traffic on that socket. Firecracker only
delivers guest-initiated AF_VSOCK connections to a *listening* Unix
socket at ``<uds_path>_<port>``, so every read_file silently failed
over to the slow serial console and, after ~4.5h of load, the
interleaved debug spam on stderr wedged the serial console entirely.

We simulate Firecracker's guest-forwarding side with a loopback Unix
socket: whatever thread plays "the guest" connects to
``<uds_path>_<port>`` directly; the real VsockHostListener accepts it
and handles the JSON protocol.
"""

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.vsock import VsockHostListener


def _fake_guest_upload(listener_path: str, payload: bytes, checksum: str, cmd_id: str,
                      remote_path: str = "/tmp/source.bin", timeout: float = 5.0):
    """Connect to the listener as the guest would and upload ``payload``.

    Mirrors the guest agent's ``handle_vsock_upload_to_host``: connect,
    send JSON upload request, wait for READY, stream bytes, wait for
    COMPLETE/ERROR.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(listener_path)
    try:
        s.sendall(
            (
                json.dumps(
                    {
                        "type": "upload",
                        "path": remote_path,
                        "size": len(payload),
                        "checksum": checksum,
                        "cmd_id": cmd_id,
                    }
                )
                + "\n"
            ).encode()
        )

        # Read READY
        buffer = b""
        while b"\n" not in buffer:
            buffer += s.recv(4096)
        ready_line, buffer = buffer.split(b"\n", 1)
        ready = json.loads(ready_line)
        assert ready["type"] == "ready", ready

        # Stream payload
        s.sendall(payload)

        # Read COMPLETE / ERROR
        while b"\n" not in buffer:
            buffer += s.recv(4096)
        final_line, _ = buffer.split(b"\n", 1)
        return json.loads(final_line)
    finally:
        s.close()


class TestVsockHostListenerPendingUploads:
    """Validates the pending-upload registration path used by download_file.

    The agent uploads a file under a pre-agreed ``cmd_id``; the listener
    must route that data to the ``local_path`` registered before the
    request was sent, not the guest-supplied ``request.path``.
    """

    def test_upload_routes_to_registered_local_path(self, tmp_path):
        uds_base = str(tmp_path / "vsock_vm.sock")
        local_dest = tmp_path / "downloaded.bin"

        listener = VsockHostListener(uds_path=uds_base, port=9000)
        listener.start()
        try:
            cmd_id = "test-cmd-123"
            listener.register_pending_upload(cmd_id, str(local_dest))

            payload = b"hello vsock " * 4096  # ~48KB, > CHUNK_SIZE
            checksum = hashlib.md5(payload).hexdigest()

            result = _fake_guest_upload(
                listener.listener_path, payload, checksum, cmd_id,
                remote_path="/some/guest/path/original.bin",
            )

            assert result["type"] == "complete", result
            assert result["size"] == len(payload)
            assert local_dest.exists(), (
                "pending upload registration should redirect the upload "
                "to the caller's local_path"
            )
            assert local_dest.read_bytes() == payload

            # Pending upload must be auto-unregistered on success so the
            # listener doesn't pin a stale reference.
            assert listener.get_pending_upload_path(cmd_id) is None
        finally:
            listener.stop()

    def test_unregister_on_timeout_frees_slot(self, tmp_path):
        uds_base = str(tmp_path / "vsock_vm.sock")
        listener = VsockHostListener(uds_path=uds_base, port=9001)
        listener.start()
        try:
            listener.register_pending_upload("cmd-a", str(tmp_path / "a.bin"))
            assert listener.get_pending_upload_path("cmd-a") == str(tmp_path / "a.bin")
            listener.unregister_pending_upload("cmd-a")
            assert listener.get_pending_upload_path("cmd-a") is None
        finally:
            listener.stop()

    def test_concurrent_uploads_route_to_their_own_paths(self, tmp_path):
        """Multiple parallel uploads with different cmd_ids must not cross wires.

        Regression for the scenario in log 4c99a657: 3 concurrent
        read_file requests arriving at the same time. With the old
        shared-socket design, a concurrent ``vsock_disconnect`` from one
        thread would kill the socket out from under the others. With a
        per-transfer socket + per-connection handler thread in the
        listener, this is structurally impossible.
        """
        uds_base = str(tmp_path / "vsock_vm.sock")
        listener = VsockHostListener(uds_path=uds_base, port=9002)
        listener.start()
        try:
            N = 6
            payloads = {
                f"cmd-{i}": (b"payload-%d-" % i) * (2048 + i * 100)
                for i in range(N)
            }
            destinations = {
                cmd_id: str(tmp_path / f"out-{cmd_id}.bin")
                for cmd_id in payloads
            }
            for cmd_id, dest in destinations.items():
                listener.register_pending_upload(cmd_id, dest)

            results = {}
            errors = []

            def upload(cmd_id):
                try:
                    payload = payloads[cmd_id]
                    checksum = hashlib.md5(payload).hexdigest()
                    results[cmd_id] = _fake_guest_upload(
                        listener.listener_path, payload, checksum, cmd_id,
                        remote_path=f"/guest/{cmd_id}",
                        timeout=10.0,
                    )
                except Exception as e:
                    errors.append((cmd_id, e))

            threads = [
                threading.Thread(target=upload, args=(cid,), daemon=True)
                for cid in payloads
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert not errors, f"upload errors: {errors}"
            assert len(results) == N
            for cmd_id, payload in payloads.items():
                assert results[cmd_id]["type"] == "complete"
                dest = Path(destinations[cmd_id])
                assert dest.exists(), f"{cmd_id} did not land on disk"
                assert dest.read_bytes() == payload, (
                    f"{cmd_id} content mismatch — cross-wire bug?"
                )
        finally:
            listener.stop()

    def test_checksum_mismatch_surfaces_error(self, tmp_path):
        uds_base = str(tmp_path / "vsock_vm.sock")
        dest = tmp_path / "out.bin"
        listener = VsockHostListener(uds_path=uds_base, port=9003)
        listener.start()
        try:
            listener.register_pending_upload("bad-cmd", str(dest))
            payload = b"x" * 1024
            result = _fake_guest_upload(
                listener.listener_path, payload, checksum="deadbeef" * 4,
                cmd_id="bad-cmd",
            )
            assert result["type"] == "error"
            assert "checksum" in result["error"].lower()
            # On checksum failure we must NOT persist the bogus file, or the
            # caller could end up reading half-transferred bytes.
            assert not dest.exists()
        finally:
            listener.stop()


class TestVsockHostListenerLifecycle:
    """Exercise the listener's bind/stop invariants."""

    def test_listener_socket_created_and_removed(self, tmp_path):
        uds_base = str(tmp_path / "vsock_vm.sock")
        listener = VsockHostListener(uds_path=uds_base, port=9010)

        assert not os.path.exists(listener.listener_path)
        listener.start()
        try:
            # Socket must exist at uds_base_PORT so Firecracker can route
            # guest connections here. This is the whole reason the old
            # host-initiated bridge couldn't work.
            assert os.path.exists(listener.listener_path)
            mode = os.stat(listener.listener_path).st_mode
            import stat
            assert stat.S_ISSOCK(mode)
        finally:
            listener.stop()

        assert not os.path.exists(listener.listener_path), (
            "listener must unlink its socket on stop to avoid EADDRINUSE "
            "on the next snapshot restore"
        )

    def test_start_cleans_stale_socket(self, tmp_path):
        uds_base = str(tmp_path / "vsock_vm.sock")
        stale_path = f"{uds_base}_9011"
        Path(stale_path).parent.mkdir(parents=True, exist_ok=True)
        # Leave a plain file where the socket should go
        Path(stale_path).write_text("stale")

        listener = VsockHostListener(uds_path=uds_base, port=9011)
        try:
            listener.start()  # must clean up the stale file
            assert os.path.exists(stale_path)
            mode = os.stat(stale_path).st_mode
            import stat
            assert stat.S_ISSOCK(mode), (
                "a stale file must be unlinked and replaced with a fresh "
                "Unix socket, not rejected"
            )
        finally:
            listener.stop()

    def test_double_start_is_safe(self, tmp_path):
        uds_base = str(tmp_path / "vsock_vm.sock")
        listener = VsockHostListener(uds_path=uds_base, port=9012)
        try:
            listener.start()
            # Second start must not raise / must not double-bind.
            listener.start()
            assert os.path.exists(listener.listener_path)
        finally:
            listener.stop()

    def test_stop_without_start_is_noop(self, tmp_path):
        uds_base = str(tmp_path / "vsock_vm.sock")
        listener = VsockHostListener(uds_path=uds_base, port=9013)
        # Must not raise.
        listener.stop()
