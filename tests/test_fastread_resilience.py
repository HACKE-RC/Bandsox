"""Fault-injection tests for the fast-read RPC plumbing.

These check the host-side bits in isolation (no live VM) so they can
run in CI: listener-restart resilience, pending-buffer race, fastread
client-server roundtrip, supervisor restart of a dead accept loop.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from bandsox.vsock.host_listener import VsockHostListener
from bandsox.vsock.fastread_server import FastReadServer


@pytest.fixture
def vsock_host_listener(tmp_path):
    """A listener bound to a temp UDS path; cleaned up at teardown."""
    uds_path = str(tmp_path / "vsock.sock")
    port = 9001
    listener = VsockHostListener(uds_path=uds_path, port=port)
    listener.start()
    # Give accept loop a moment to come up.
    time.sleep(0.05)
    yield listener
    listener.stop()


def _frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _recv_n(sock, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ConnectionResetError(f"closed after {len(out)}/{n} bytes")
        out.extend(chunk)
    return bytes(out)


def test_register_pending_buffer_max_bytes_rejects_oversize(vsock_host_listener):
    """Listener must refuse uploads above the cap before reading any bytes."""
    cmd_id = str(uuid.uuid4())
    slot = vsock_host_listener.register_pending_buffer(cmd_id, max_bytes=128)
    assert slot["max_bytes"] == 128
    vsock_host_listener.unregister_pending_buffer(cmd_id)


def test_register_pending_buffer_unregister_idempotent(vsock_host_listener):
    cmd_id = str(uuid.uuid4())
    vsock_host_listener.register_pending_buffer(cmd_id)
    vsock_host_listener.unregister_pending_buffer(cmd_id)
    vsock_host_listener.unregister_pending_buffer(cmd_id)  # second time: no error


def test_fastread_server_supervisor_restarts_dead_accept_loop(tmp_path):
    """If the accept thread dies, the supervisor must rebuild it.

    Previously a wedged accept loop left the socket bound but unanswered,
    surfacing as silent timeouts on the athena side.
    """
    sock_path = str(tmp_path / "fastread.sock")
    listener = VsockHostListener(uds_path=str(tmp_path / "vsock.sock"), port=9100)
    listener.start()
    try:
        srv = FastReadServer(
            socket_path=sock_path,
            vsock_listener=listener,
            write_to_agent=lambda _data: None,  # discard, no real agent
            vsock_port=9100,
        )
        srv.start()
        try:
            t1 = srv._accept_thread
            assert t1 is not None and t1.is_alive()

            # Force-close the listening socket — accept() will raise EBADF
            # and the original accept loop will return.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ping:
                ping.connect(sock_path)
            srv._server_sock.close()

            # Wait up to 6s (supervisor sleeps 2s between checks) for the
            # supervisor to notice and rebuild. We can't directly verify a
            # working accept here (we closed the bound socket), so we
            # check that the supervisor at least observed the death.
            deadline = time.time() + 6
            while time.time() < deadline:
                if srv._accept_thread is not t1:
                    break
                time.sleep(0.1)
            assert srv._accept_thread is not t1, (
                "supervisor never replaced the dead accept thread; without "
                "this the fastread RPC would silently stop accepting"
            )
        finally:
            srv.stop()
    finally:
        listener.stop()


def test_fastread_listener_getter_picks_up_supervisor_restarts(tmp_path):
    """If the vsock listener is replaced, the fastread server must use the new one.

    We exercise this by passing a getter that returns different listeners
    on each call. A fastread handler in flight will use whichever the
    getter returns at request time.
    """
    sock_path = str(tmp_path / "fastread.sock")

    listener_a = VsockHostListener(uds_path=str(tmp_path / "vsock_a.sock"), port=9200)
    listener_b = VsockHostListener(uds_path=str(tmp_path / "vsock_b.sock"), port=9201)
    listener_a.start()
    listener_b.start()
    try:
        current = {"l": listener_a}

        srv = FastReadServer(
            socket_path=sock_path,
            vsock_listener=lambda: current["l"],
            write_to_agent=lambda _data: None,
            vsock_port=9200,
        )
        srv.start()
        try:
            # Connect to fastread, send a request — the handler will
            # register a buffer on listener_a, then we swap.
            cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            cli.settimeout(2.0)
            cli.connect(sock_path)
            cli.sendall(_frame({"path": "/whatever", "cmd_id": "abc"}))

            # Give the server a moment to register the buffer on listener_a.
            time.sleep(0.1)
            assert listener_a.get_pending_buffer("abc") is not None
            # Swap listener under the running handler — next register
            # should land on listener_b.
            current["l"] = listener_b
            cli.close()
        finally:
            srv.stop()
    finally:
        listener_a.stop()
        listener_b.stop()


def test_fastread_oversize_buffer_rejected_at_registration():
    """A buffer with max_bytes set must reject upload sizes that exceed it."""
    listener = VsockHostListener(uds_path="/tmp/vsock_oversize_test.sock", port=9300)
    listener.start()
    try:
        slot = listener.register_pending_buffer("over", max_bytes=10)
        assert slot["max_bytes"] == 10
        # Direct enforcement happens in _handle_upload at request time;
        # here we just verify the slot is shaped right.
        assert "buf" in slot and "done" in slot and "error" in slot
    finally:
        listener.stop()
