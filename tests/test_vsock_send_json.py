"""Regression test for vsock_send_json line-level atomicity.

Same class of bug as the send_event interleave fix in commit 0a1d035:
multiple worker threads emitting JSON-per-line on a shared socket can
interleave their writes, corrupting the framing the host parser relies on.
A corrupted line means the matching response/event is dropped and the
caller hangs until timeout.

vsock_send_json is called from handle_vsock_download once per chunk, so any
concurrent vsock activity (multiple downloads, or download + status) can
race. The fix wraps sock.sendall in a dedicated write lock.
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


def test_concurrent_vsock_send_json_does_not_interleave():
    """N threads sending M JSON messages each must produce M*N parseable lines.

    Uses a real socketpair (not StringIO) so concurrent sendall sees actual
    multi-thread racing on the kernel-side socket buffer. With the bug
    (sendall outside the write lock), large payloads occasionally split
    across two threads' writes and produce unparseable lines.
    """
    parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    # Force sendall to split into multiple syscalls. Without this the
    # default Unix-socketpair kernel buffer (~256KB) absorbs each message
    # in one send() and the GIL never gets released mid-message, hiding
    # the race. Setting SNDBUF small + RCVBUF small means a 4KB payload
    # round-trips through several send() calls per sendall, which is the
    # only condition under which two threads' bytes can interleave.
    child_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
    parent_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)

    captured_bytes = bytearray()
    drain_done = threading.Event()

    def drain():
        try:
            while True:
                chunk = parent_sock.recv(65536)
                if not chunk:
                    return
                captured_bytes.extend(chunk)
        except Exception:
            return
        finally:
            drain_done.set()

    drain_t = threading.Thread(target=drain, daemon=True)
    drain_t.start()

    # Patch the agent globals so vsock_send_json uses our socketpair.
    original_socket = agent.VSOCK_SOCKET
    original_enabled = agent.VSOCK_ENABLED
    agent.VSOCK_SOCKET = child_sock
    agent.VSOCK_ENABLED = True

    try:
        N_THREADS = 8
        M_EVENTS = 200

        # Payload large enough that a single sendall is likely to span
        # multiple syscalls / scheduler slices, maximising interleave odds
        # under the unpatched code path.
        big_payload = "x" * 4096

        def worker(tid):
            for i in range(M_EVENTS):
                agent.vsock_send_json(
                    {
                        "type": "status",
                        "payload": {
                            "cmd_id": f"cmd-{tid}",
                            "type": "chunk",
                            "data": big_payload,
                            "tid": tid,
                            "i": i,
                        },
                    }
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        # Close the child side so the drain thread sees EOF and exits.
        child_sock.close()
        drain_done.wait(timeout=2.0)
        parent_sock.close()
        agent.VSOCK_SOCKET = original_socket
        agent.VSOCK_ENABLED = original_enabled

    text = captured_bytes.decode("utf-8")
    lines = text.split("\n")
    # Last element is the trailing empty string after the final newline.
    assert lines[-1] == "", f"unexpected trailing data: {lines[-1]!r}"
    lines = lines[:-1]

    assert len(lines) == N_THREADS * M_EVENTS, (
        f"expected {N_THREADS * M_EVENTS} lines, got {len(lines)}"
    )

    seen = set()
    for idx, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"line {idx} is not valid JSON (interleave bug): {line!r}"
            ) from exc
        payload = obj.get("payload", {})
        seen.add((payload.get("cmd_id"), payload.get("i")))

    assert len(seen) == N_THREADS * M_EVENTS, (
        f"duplicate or missing (cmd_id,i) pairs: {len(seen)} unique"
    )
