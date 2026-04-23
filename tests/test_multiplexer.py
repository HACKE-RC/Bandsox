"""Tests for ConsoleMultiplexer broadcast safety.

These exercise the regression where a wedged client socket or a slow
callback would block the stdout drain thread (because the lock was held
during blocking sendall()). Symptoms in production: after hours of use
every guest command times out, including `echo ok`.
"""

import io
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.vm import ConsoleMultiplexer  # noqa: E402


class FakeProcess:
    """Minimal subprocess.Popen stand-in for the multiplexer."""

    def __init__(self, stdout_lines):
        self._lines = list(stdout_lines)
        self.stdout = io.StringIO()
        # readline() on StringIO will see only what's been written so far.
        # We prime it up front because _read_stdout_loop loops until EOF.
        for line in self._lines:
            self.stdout.write(line)
        self.stdout.seek(0)
        self.stdin = io.StringIO()
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False


def _make_mux(tmp_path, stdout_lines):
    socket_path = str(tmp_path / "console.sock")
    proc = FakeProcess(stdout_lines)
    mux = ConsoleMultiplexer(socket_path, proc)
    return mux, proc, socket_path


def test_callbacks_fire_outside_lock(tmp_path):
    """A slow callback must not block further broadcasts."""
    lines = [f"line{i}\n" for i in range(5)]
    mux, proc, sock_path = _make_mux(tmp_path, lines)

    received = []
    barrier = threading.Event()

    def slow_first_callback(line):
        # Block the first call briefly; if the mux held its lock, the
        # stdout loop would stall for this entire duration.
        if not barrier.is_set():
            time.sleep(0.3)
            barrier.set()
        received.append(line)

    mux.add_callback(slow_first_callback)
    mux.start()

    try:
        # All 5 lines should arrive within well under the 5*0.3s that
        # holding-the-lock behavior would require.
        deadline = time.time() + 2.0
        while len(received) < 5 and time.time() < deadline:
            time.sleep(0.05)
        assert len(received) == 5, received
    finally:
        mux.stop()
        proc.terminate()


def test_slow_client_does_not_block_drain(tmp_path):
    """A client that stops reading must not freeze the stdout drain.

    The fix uses a 2s settimeout on per-client sendall and drops the
    client on timeout. Prior behavior would block indefinitely and hold
    the lock, preventing any further firecracker stdout from draining.
    """
    proc = PipeBackedProcess()
    sock_path = str(tmp_path / "console.sock")
    mux = ConsoleMultiplexer(sock_path, proc)

    received_via_cb = []
    mux.add_callback(received_via_cb.append)

    mux.start()
    deadline = time.time() + 2.0
    while not os.path.exists(sock_path) and time.time() < deadline:
        time.sleep(0.01)

    # Silent client connects with a tiny recv buffer so we're sure to
    # fill it within a few big writes. On Linux SO_RCVBUF has a minimum
    # the kernel enforces (~2KB or so), which still fills very quickly.
    silent_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent_client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    silent_client.connect(sock_path)
    # Give the accept thread a beat to register the client.
    time.sleep(0.1)

    try:
        # Feed a big stream of large lines. With a 4KB recv buffer on
        # the silent client and 64KB lines, the send buffer fills on
        # the very first send. Old code blocked forever here.
        big_line = ("x" * 65536) + "\n"
        n_lines = 40
        t0 = time.time()
        for _ in range(n_lines):
            proc.write_line(big_line)

        deadline = time.time() + 8.0
        while len(received_via_cb) < n_lines and time.time() < deadline:
            time.sleep(0.05)
        elapsed = time.time() - t0

        assert len(received_via_cb) == n_lines, (
            f"drain stalled: got {len(received_via_cb)}/{n_lines} lines "
            f"after {elapsed:.1f}s"
        )
        # Old code would either hang forever (test times out at pytest
        # level) or, with settimeout=2s fix, complete within a few
        # seconds since the stalled client is dropped once.
        assert elapsed < 8.0, f"drain too slow: {elapsed:.1f}s"
    finally:
        try:
            silent_client.close()
        except Exception:
            pass
        mux.stop()
        proc.terminate()


class PipeBackedProcess:
    """Fake process whose stdout is a real pipe we can write into later."""

    def __init__(self):
        r_fd, w_fd = os.pipe()
        self.stdout = os.fdopen(r_fd, "r")
        self._w = os.fdopen(w_fd, "w")
        self.stdin = io.StringIO()
        self._alive = True

    def write_line(self, line):
        self._w.write(line)
        self._w.flush()

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False
        try:
            self._w.close()
        except Exception:
            pass


def test_healthy_client_receives_broadcast(tmp_path):
    """A client that reads promptly should receive every line."""
    proc = PipeBackedProcess()
    sock_path = str(tmp_path / "console.sock")
    mux = ConsoleMultiplexer(sock_path, proc)

    mux.start()
    deadline = time.time() + 2.0
    while not os.path.exists(sock_path) and time.time() < deadline:
        time.sleep(0.01)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    client.settimeout(2.0)

    # Give the accept thread a moment to register the client.
    time.sleep(0.05)

    lines = [f"event{i}\n" for i in range(10)]
    for line in lines:
        proc.write_line(line)

    try:
        buf = b""
        expected = "".join(lines).encode()
        deadline = time.time() + 3.0
        while len(buf) < len(expected) and time.time() < deadline:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        assert buf == expected, f"expected {expected!r} got {buf!r}"
    finally:
        client.close()
        mux.stop()
        proc.terminate()
