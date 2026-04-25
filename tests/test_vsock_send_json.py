"""Regression test for concurrent vsock JSON writes.

Historically the guest agent kept a single VSOCK_SOCKET global and had
multiple worker threads call ``sock.sendall`` on it concurrently, which
interleaved bytes at the kernel level when payloads exceeded the socket
buffer. The host parser saw corrupted JSON and dropped events, hanging
the caller.

The fix is architectural: each vsock transfer now uses its own
short-lived socket (``vsock_create_connection`` → send → close), and the
single-message helper ``vsock_send_json_msg`` takes the socket as an
explicit argument. No global socket, no shared writer, no interleave
possible.

This test encodes the new invariant: N worker threads each opening their
own socketpair and calling ``vsock_send_json_msg`` produce exactly N×M
parseable lines with no cross-thread corruption, even under
deliberately-small SO_SNDBUF.
"""

import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path


def _load_agent_module():
    path = Path(__file__).resolve().parents[1] / "bandsox" / "agent.py"
    spec = importlib.util.spec_from_file_location("bandsox_agent_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agent = _load_agent_module()


def test_per_transfer_socket_has_no_shared_writer():
    """Each transfer uses its own socket → no writer to contend on.

    If a regression re-introduces a module-global socket and unsynchronised
    writer, this test is still here to ensure the helper used by every
    transfer (vsock_send_json_msg) accepts an explicit socket argument.
    """
    assert hasattr(agent, "vsock_send_json_msg")
    assert not hasattr(agent, "VSOCK_SOCKET"), (
        "agent must not expose a module-global shared vsock socket; each "
        "transfer must use its own short-lived socket (see the Broken pipe "
        "cascade in session 4c99a657)"
    )


def test_concurrent_per_socket_writes_are_line_atomic():
    """N threads writing to N independent socketpairs produce clean JSON lines.

    Small SO_SNDBUF forces sendall to split across syscalls; with a shared
    socket the old agent would interleave bytes here. With per-transfer
    sockets the kernel serialises each socket's writes and we get clean
    output every time.
    """
    N_THREADS = 8
    M_EVENTS = 50
    big_payload = "x" * 4096

    collected = {}
    errors = []

    def worker(tid):
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        child_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        parent_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)

        captured = bytearray()
        drain_done = threading.Event()

        def drain():
            try:
                while True:
                    chunk = parent_sock.recv(65536)
                    if not chunk:
                        return
                    captured.extend(chunk)
            finally:
                drain_done.set()

        t = threading.Thread(target=drain, daemon=True)
        t.start()

        try:
            for i in range(M_EVENTS):
                agent.vsock_send_json_msg(
                    child_sock,
                    {
                        "type": "status",
                        "payload": {
                            "cmd_id": f"cmd-{tid}",
                            "type": "chunk",
                            "data": big_payload,
                            "tid": tid,
                            "i": i,
                        },
                    },
                )
        except Exception as e:
            errors.append(e)
        finally:
            child_sock.close()
            drain_done.wait(timeout=2.0)
            parent_sock.close()

        collected[tid] = bytes(captured)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"worker exceptions: {errors}"
    assert len(collected) == N_THREADS

    for tid, data in collected.items():
        text = data.decode("utf-8")
        lines = [ln for ln in text.split("\n") if ln]
        assert len(lines) == M_EVENTS, (
            f"thread {tid}: expected {M_EVENTS} lines, got {len(lines)}"
        )
        for idx, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"thread {tid} line {idx} corrupted: {line[:200]!r}"
                ) from exc
            payload = obj.get("payload", {})
            assert payload.get("tid") == tid, (
                f"thread {tid} saw foreign tid {payload.get('tid')} "
                f"(interleave bug reintroduced)"
            )
            assert payload.get("i") == idx
