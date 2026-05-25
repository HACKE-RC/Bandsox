"""Tests for best-effort guest RNG unblocking on snapshot restore."""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.core import BandSox


def _make_bandsox_without_init():
    return BandSox.__new__(BandSox)


def test_unblock_rng_runs_both_shell_mix_and_python_paths():
    """Both the portable shell mix AND the python3 RNDADDENTROPY ioctl attempt
    must be invoked. Shell mix guarantees snapshot-restore divergence even on
    images without python3 (e.g. the claude-code template). Python path adds
    entropy crediting when available."""
    bs = _make_bandsox_without_init()
    calls = []

    def exec_command(cmd, timeout=0):
        calls.append((cmd, timeout))
        return 0

    vm = SimpleNamespace(vm_id="vm-probe-ok", exec_command=exec_command)
    bs._best_effort_unblock_guest_rng(vm)

    assert len(calls) == 2

    shell_cmd = calls[0][0]
    assert "base64 -d" in shell_cmd
    assert "/dev/urandom" in shell_cmd
    assert "/dev/random" in shell_cmd
    # The shell command must NOT depend on python or other interpreters.
    assert "python" not in shell_cmd

    python_cmd = calls[1][0]
    assert "command -v python3" in python_cmd
    assert "RNDADDENTROPY" in python_cmd
    assert "base64.b64decode" in python_cmd


def test_unblock_rng_distinct_seeds_between_calls():
    """The host_seed used at each call must be fresh entropy: two consecutive
    calls must inject distinct seeds. Otherwise restored copies would still
    converge if our seed source had a stuck state."""
    bs = _make_bandsox_without_init()
    seeds_seen = []

    def exec_command(cmd, timeout=0):
        # Capture the base64 blob from the shell mix command (first arg of
        # each printf), regardless of which call this is.
        import re
        m = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", cmd)
        if m:
            seeds_seen.append(m.group(1))
        return 0

    vm = SimpleNamespace(vm_id="vm-distinct", exec_command=exec_command)
    bs._best_effort_unblock_guest_rng(vm)
    bs._best_effort_unblock_guest_rng(vm)

    # First call: shell+python both capture the same seed. Second call: same.
    # Across calls, seeds must differ.
    assert len(set(seeds_seen)) >= 2, f"seeds did not diverge: {seeds_seen}"


def test_unblock_rng_never_raises_on_nonzero_injection():
    bs = _make_bandsox_without_init()
    calls = []

    def exec_command(cmd, timeout=0):
        calls.append((cmd, timeout))
        return 1

    vm = SimpleNamespace(vm_id="vm-probe-fail", exec_command=exec_command)
    bs._best_effort_unblock_guest_rng(vm)

    # Even when both paths return non-zero, we must not raise.
    assert len(calls) == 2


def test_unblock_rng_never_raises_on_exec_errors():
    bs = _make_bandsox_without_init()

    class Boom(Exception):
        pass

    def exec_command(_cmd, timeout=0):
        raise Boom("serial link temporarily unavailable")

    vm = SimpleNamespace(vm_id="vm-boom", exec_command=exec_command)
    # Must be fully best-effort even when every exec attempt blows up.
    bs._best_effort_unblock_guest_rng(vm)


def test_unblock_rng_continues_to_python_path_when_shell_path_raises():
    """If the portable shell mix throws, the python3 attempt must still run."""
    bs = _make_bandsox_without_init()
    calls = []

    def exec_command(cmd, timeout=0):
        calls.append(cmd)
        if "base64 -d" in cmd:
            raise RuntimeError("serial flap during shell mix")
        return 0

    vm = SimpleNamespace(vm_id="vm-flaky-shell", exec_command=exec_command)
    bs._best_effort_unblock_guest_rng(vm)

    assert len(calls) == 2
    assert "RNDADDENTROPY" in calls[1]
