"""End-to-end integration test: guest agent path → host listener.

Wires the real ``handle_vsock_upload_to_host`` from the guest agent to
the real ``VsockHostListener`` using a TCP socketpair as a stand-in for
Firecracker's vsock forwarding. If any piece of the protocol regresses
(framing, checksum handling, pending-upload routing), this test fails.

This is the test that would have caught the bug in session 4c99a657
before it shipped: the old bridge code couldn't complete even one
round-trip, because it tried to receive on a connect()ed socket instead
of an accept()ed listener.
"""

import hashlib
import importlib.util
import io
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


def _load_agent_module():
    path = Path(__file__).resolve().parents[1] / "bandsox" / "agent.py"
    spec = importlib.util.spec_from_file_location(
        "bandsox_agent_e2e", path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGuestAgentAgainstRealListener:
    """End-to-end: agent.handle_vsock_upload_to_host → VsockHostListener.

    We monkeypatch the agent's ``vsock_create_connection`` to return a
    Unix socket connected to our listener, then call the real upload
    handler. The listener must route the bytes to the pre-registered
    destination and emit the "uploaded" status event the caller expects.
    """

    def _run_upload(self, tmp_path, payload: bytes, cmd_id: str = "e2e-cmd"):
        agent = _load_agent_module()

        uds_base = str(tmp_path / "vsock_vm.sock")
        source_path = tmp_path / "source.bin"
        dest_path = tmp_path / "downloaded.bin"
        source_path.write_bytes(payload)

        listener = VsockHostListener(uds_path=uds_base, port=9000)
        listener.start()

        # Redirect agent stdout/stderr to buffers so we can inspect the
        # events it emits over the simulated serial console.
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        orig_stdout = agent.sys.stdout
        orig_stderr = agent.sys.stderr
        agent.sys.stdout = stdout_buf
        agent.sys.stderr = stderr_buf

        try:
            listener.register_pending_upload(cmd_id, str(dest_path))

            def fake_create_connection(port, timeout=10.0):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(listener.listener_path)
                return s

            # Patch the agent's connection factory + mark vsock as
            # working so the handler doesn't prematurely fall back.
            orig_create = agent.vsock_create_connection
            agent.vsock_create_connection = fake_create_connection
            with agent._vsock_available_lock:
                agent._vsock_available = True
                agent._vsock_last_probe_ts = time.time()

            try:
                agent.handle_vsock_upload_to_host(cmd_id, str(source_path))
            finally:
                agent.vsock_create_connection = orig_create
        finally:
            agent.sys.stdout = orig_stdout
            agent.sys.stderr = orig_stderr
            listener.stop()

        return stdout_buf.getvalue(), stderr_buf.getvalue(), dest_path

    def test_small_file_round_trip(self, tmp_path):
        payload = b"hello vsock end-to-end"
        events_out, _err, dest = self._run_upload(tmp_path, payload)

        # The handler must emit "uploaded" + "exit 0" events on success.
        events = [json.loads(line) for line in events_out.splitlines() if line]
        kinds = [(e["type"], e["payload"].get("status")) for e in events]
        assert ("status", "uploaded") in kinds
        assert ("exit", None) in kinds
        assert dest.read_bytes() == payload

    def test_large_file_round_trip(self, tmp_path):
        # Larger than CHUNK_SIZE (64KB) to exercise multi-recv paths.
        payload = (b"abc123" * 4096) + b"tail"  # ~24KB
        payload = payload * 8  # ~200KB
        events_out, _err, dest = self._run_upload(tmp_path, payload)

        events = [json.loads(line) for line in events_out.splitlines() if line]
        assert any(
            e["type"] == "status" and e["payload"].get("status") == "uploaded"
            for e in events
        )
        assert dest.read_bytes() == payload
        # Checksum must match what was sent — any corruption would be
        # caught by VsockHostListener and surface as an ERROR event.
        assert hashlib.md5(dest.read_bytes()).hexdigest() == hashlib.md5(
            payload
        ).hexdigest()

    def test_vsock_unavailable_falls_back_to_serial(self, tmp_path):
        """If vsock is unavailable the handler should call handle_read_file."""
        agent = _load_agent_module()

        source = tmp_path / "tiny.bin"
        source.write_bytes(b"x" * 100)

        # Force vsock_create_connection to return None (simulating a
        # broken vsock device — exactly the state session 4c99a657
        # was stuck in for 4.5h).
        orig_create = agent.vsock_create_connection
        agent.vsock_create_connection = lambda port, timeout=10.0: None

        stdout_buf = io.StringIO()
        orig_stdout = agent.sys.stdout
        agent.sys.stdout = stdout_buf

        try:
            agent.handle_vsock_upload_to_host("fb-cmd", str(source))
        finally:
            agent.vsock_create_connection = orig_create
            agent.sys.stdout = orig_stdout

        events = [
            json.loads(line) for line in stdout_buf.getvalue().splitlines() if line
        ]
        # Serial fallback uses file_content (small file) + exit. The vsock
        # "uploaded" status must NOT be present.
        assert any(e["type"] == "file_content" for e in events), events
        assert not any(
            e["type"] == "status" and e["payload"].get("status") == "uploaded"
            for e in events
        )
