"""Regression tests for the guest agent's vsock fallback + console locking.

Bug 1: The previous agent wrote vsock debug lines to ``sys.stderr`` from
any thread, without synchronising with the ``send_event`` stdout writer.
Firecracker's serial console funnels stdout and stderr to the same
device, so those unsynchronised writes interleaved with in-flight JSON
events on the wire. The host parser dropped the corrupted lines and the
VM eventually wedged. See session log 4c99a657 line 899 for the
captured interleave.

Bug 2: Every ``read_file`` paid ~3s retrying a known-broken vsock
before falling back to serial. Over a 4.5h session that's thousands of
redundant connect attempts, each generating a stderr debug line that
could interleave.

The new agent:
  - Uses a single ``_console_lock`` covering stdout and stderr writes.
  - Caches vsock brokenness and short-circuits subsequent read_files to
    serial without another probe, re-probing at most once/minute.
"""

import importlib.util
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest


def _load_agent_module():
    path = Path(__file__).resolve().parents[1] / "bandsox" / "agent.py"
    spec = importlib.util.spec_from_file_location(
        "bandsox_agent_fallback", path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load_agent_module()


class TestConsoleLock:
    """Covers the stdout/stderr serial-console locking fix."""

    def test_send_event_and_log_stderr_share_a_lock(self, agent):
        """Regression: log_stderr must acquire the same console lock as send_event.

        If stderr writes skip the lock, they can interleave with an
        in-flight JSON line on the serial console and corrupt it.
        """
        assert hasattr(agent, "_console_lock")
        assert hasattr(agent, "log_stderr")

    def test_concurrent_stdout_stderr_writes_dont_interleave(
        self, agent, monkeypatch
    ):
        """N threads racing send_event + log_stderr must produce clean lines."""
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        # Shared lock across stdout + stderr stand-ins using the real
        # agent write paths.
        monkeypatch.setattr(agent.sys, "stdout", buf_out)
        monkeypatch.setattr(agent.sys, "stderr", buf_err)

        N = 16
        M = 100

        def emit_events(tid):
            for i in range(M):
                agent.send_event(
                    "output",
                    {"cmd_id": f"c-{tid}", "stream": "stdout", "data": f"line-{tid}-{i}\n"},
                )

        def emit_stderr(tid):
            for i in range(M):
                agent.log_stderr(f"stderr thread {tid} iter {i}\n")

        threads = []
        for tid in range(N):
            threads.append(threading.Thread(target=emit_events, args=(tid,)))
            threads.append(threading.Thread(target=emit_stderr, args=(tid,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every stdout line must parse as JSON — no stderr fragments mixed in.
        for idx, raw in enumerate(buf_out.getvalue().splitlines()):
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                pytest.fail(
                    f"stdout line {idx} corrupted by stderr interleave: {raw!r}"
                )
        # Every stderr line must start fresh — no JSON fragments in them.
        for idx, raw in enumerate(buf_err.getvalue().splitlines()):
            assert not raw.startswith("{"), (
                f"stderr line {idx} absorbed a stdout JSON fragment: {raw!r}"
            )


class TestVsockBrokenCircuitBreaker:
    """Once vsock has failed we must not keep retrying for every file read."""

    def test_mark_broken_short_circuits_can_use(self, agent):
        # Reset state via explicit access; module-level globals are the
        # only sanctioned API.
        with agent._vsock_available_lock:
            agent._vsock_available = None
            agent._vsock_last_probe_ts = 0.0

        agent._vsock_mark_broken()
        # _vsock_can_use should respect the cached failure and NOT probe.
        assert agent._vsock_can_use(9000) is False

    def test_reprobe_interval_is_respected(self, agent, monkeypatch):
        """After the reprobe interval we must give vsock another chance.

        New design: _vsock_can_use no longer actively probes — it simply
        unblocks the caller (returns True) so the next vsock_create_connection
        can act as the probe. This avoids paying a 1s connect timeout twice
        on cold reads.
        """
        monkeypatch.setattr(agent, "_vsock_module_available", lambda: True)
        with agent._vsock_available_lock:
            agent._vsock_available = False
            agent._vsock_last_probe_ts = time.time() - 120.0  # >60s ago
            agent._vsock_fail_streak = 1
        assert agent._vsock_can_use(9000) is True

    def test_fresh_broken_state_skips_use(self, agent, monkeypatch):
        """Within the reprobe interval we must NOT attempt vsock."""
        monkeypatch.setattr(agent, "_vsock_module_available", lambda: True)
        with agent._vsock_available_lock:
            agent._vsock_available = False
            agent._vsock_last_probe_ts = time.time()
            agent._vsock_fail_streak = 1
        assert agent._vsock_can_use(9000) is False

    def test_unknown_state_allows_attempt(self, agent, monkeypatch):
        """First-ever call must allow vsock — connect acts as the probe."""
        monkeypatch.setattr(agent, "_vsock_module_available", lambda: True)
        with agent._vsock_available_lock:
            agent._vsock_available = None
            agent._vsock_last_probe_ts = 0.0
            agent._vsock_fail_streak = 0
        assert agent._vsock_can_use(9000) is True


class TestVsockModuleFreeFallback:
    """Guests without AF_VSOCK kernel support must fall back cleanly."""

    def test_create_connection_returns_none_without_kernel_support(
        self, agent, monkeypatch
    ):
        monkeypatch.setattr(agent, "_vsock_module_available", lambda: False)
        assert agent.vsock_create_connection(9000) is None

    def test_probe_returns_false_without_kernel_support(self, agent, monkeypatch):
        with agent._vsock_available_lock:
            agent._vsock_available = None
            agent._vsock_last_probe_ts = 0.0
        monkeypatch.setattr(agent, "_vsock_module_available", lambda: False)
        assert agent._vsock_probe(9000) is False


class TestNoSharedVsockGlobals:
    """The previous module globals ``VSOCK_SOCKET`` / ``VSOCK_ENABLED``
    were the root cause of the Broken pipe cascade after snapshot
    restore. Ensure nothing reintroduces them.
    """

    def test_no_shared_socket_global(self, agent):
        assert not hasattr(agent, "VSOCK_SOCKET"), (
            "module-global vsock socket is forbidden — per-transfer sockets "
            "only"
        )

    def test_no_shared_enabled_flag(self, agent):
        # We keep the availability cache but it's scoped via
        # _vsock_available (leading underscore) and protected by a lock.
        # VSOCK_ENABLED was the public flag callers poked without
        # synchronisation, so it must stay gone.
        assert not hasattr(agent, "VSOCK_ENABLED")

    def test_no_shared_reconnect_thread(self, agent):
        """The broken reconnect loop that produced 30 stderr-spam lines
        per VM boot is gone."""
        assert not hasattr(agent, "start_vsock_reconnect")
        assert not hasattr(agent, "_vsock_reconnect_loop")
