"""Regression tests for the guest agent's send_event serialization.

Before the fix, concurrent worker threads could interleave JSON lines on
the serial console (multiple sys.stdout.write/flush not protected by a
lock). A corrupt JSON line dropped on the host-side parser means the
corresponding 'exit' event is lost and the command hangs until timeout.
"""

import importlib.util
import io
import json
import sys
import threading
from pathlib import Path


def _load_agent_module():
    """Load bandsox.agent as a standalone module.

    The top-level `bandsox` package imports things (requests, uvicorn, ...)
    that aren't needed for this test. Load agent.py by path instead.
    """
    path = Path(__file__).resolve().parents[1] / "bandsox" / "agent.py"
    spec = importlib.util.spec_from_file_location("bandsox_agent_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agent = _load_agent_module()


def test_concurrent_send_event_produces_valid_json_lines(tmp_path):
    """With N threads each emitting M events, every line must be valid JSON.

    Uses a real OS pipe (not StringIO) so concurrent write+flush sees the
    actual multi-thread racing behavior. StringIO in CPython is protected
    by the GIL at the write level, which would hide the race.
    """
    import os as _os

    r_fd, w_fd = _os.pipe()
    reader = _os.fdopen(r_fd, "r", buffering=1)
    writer = _os.fdopen(w_fd, "w", buffering=1)

    # Drain the reader continuously on a thread so writes don't block
    # on the pipe buffer filling up.
    captured = []

    def drain():
        while True:
            try:
                line = reader.readline()
            except ValueError:
                return
            if not line:
                return
            captured.append(line.rstrip("\n"))

    drain_t = threading.Thread(target=drain, daemon=True)
    drain_t.start()

    original_stdout = sys.stdout
    sys.stdout = writer
    try:
        threads = []
        N_THREADS = 8
        M_EVENTS = 300

        # Large-ish payload makes a single write take multiple syscalls
        # without the lock — increasing the chance of interleaving.
        big_payload = "x" * 2048

        def worker(tid):
            for i in range(M_EVENTS):
                agent.send_event(
                    "output",
                    {
                        "cmd_id": f"cmd-{tid}",
                        "stream": "stdout",
                        "data": f"{big_payload} tid={tid} i={i}",
                    },
                )

        for t in range(N_THREADS):
            th = threading.Thread(target=worker, args=(t,))
            threads.append(th)
            th.start()
        for th in threads:
            th.join()
    finally:
        sys.stdout = original_stdout
        writer.close()

    drain_t.join(timeout=2.0)

    assert len(captured) == N_THREADS * M_EVENTS, (
        f"expected {N_THREADS * M_EVENTS} lines, got {len(captured)}"
    )

    for i, line in enumerate(captured):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            # Would fire if write+flush ever interleaved from two threads.
            raise AssertionError(f"line {i} is not valid JSON: {line!r}") from e
        assert obj.get("type") == "output"
        assert "payload" in obj
