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


def test_unblock_rng_skips_injection_when_probe_passes():
    bs = _make_bandsox_without_init()
    calls = []

    def exec_command(cmd, timeout=0):
        calls.append((cmd, timeout))
        return 0

    vm = SimpleNamespace(vm_id="vm-probe-ok", exec_command=exec_command)
    bs._best_effort_unblock_guest_rng(vm)

    assert len(calls) == 1
    assert "GRND_NONBLOCK" in calls[0][0]


def test_unblock_rng_injects_when_probe_fails():
    bs = _make_bandsox_without_init()
    calls = []

    def exec_command(cmd, timeout=0):
        calls.append((cmd, timeout))
        # Probe fails, injection succeeds.
        if len(calls) == 1:
            return 1
        return 0

    vm = SimpleNamespace(vm_id="vm-probe-fail", exec_command=exec_command)
    bs._best_effort_unblock_guest_rng(vm)

    assert len(calls) == 2
    assert "GRND_NONBLOCK" in calls[0][0]
    assert "RNDADDENTROPY" in calls[1][0]
    assert "base64.b64decode" in calls[1][0]


def test_unblock_rng_never_raises_on_exec_errors():
    bs = _make_bandsox_without_init()

    class Boom(Exception):
        pass

    def exec_command(_cmd, timeout=0):
        raise Boom("serial link temporarily unavailable")

    vm = SimpleNamespace(vm_id="vm-boom", exec_command=exec_command)
    # Must be fully best-effort.
    bs._best_effort_unblock_guest_rng(vm)
