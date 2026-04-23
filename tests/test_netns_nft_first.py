"""Regression test: netns NAT + MSS clamp must try nft before iptables-legacy.

Why this matters:

On Ubuntu 22.04+ (and other distros that have moved the kernel netfilter
backend to nftables), iptables-legacy is still installed and accepts rule
additions with returncode 0 — but the rules land in a backend the kernel
no longer consults, so they silently never fire. The MASQUERADE we install
in the netns vanishes, the MSS clamp vanishes, conntrack stays empty, and
the first HTTPS request from the guest hangs (~30s) until TCP blackhole
detection kicks in. `git clone` is the canonical user-visible symptom.

setup_netns_networking must therefore try `nft` first inside the netns,
and only fall back to iptables-legacy / iptables if nft is unavailable.
This test pins that ordering.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox import network  # noqa: E402


def _arg_after(cmd, token):
    """Return the argv element that follows `token`, or None."""
    try:
        i = cmd.index(token)
    except ValueError:
        return None
    return cmd[i + 1] if i + 1 < len(cmd) else None


def _is_netns_call(cmd, netns_name):
    return (
        "ip" in cmd
        and "netns" in cmd
        and "exec" in cmd
        and netns_name in cmd
    )


def _binary_after_netns(cmd, netns_name):
    """For an `ip netns exec <ns> <bin> ...` call, return <bin>."""
    if not _is_netns_call(cmd, netns_name):
        return None
    try:
        ns_idx = cmd.index(netns_name)
    except ValueError:
        return None
    return cmd[ns_idx + 1] if ns_idx + 1 < len(cmd) else None


@pytest.fixture
def captured_netns():
    """Patch enough of network.py that setup_netns_networking can run dry.

    Returns (run, calls). `run` invokes setup_netns_networking with the
    given fake subprocess.run side effect and returns the list of every
    captured argv. The default side effect makes every command succeed
    with returncode 0 (so nft 'wins' the race).
    """
    netns_name = "netns_test"
    tap_name = "tap_test"
    host_ip = "172.16.0.1"
    vm_id = "vm-test"

    def run(side_effect=None):
        calls = []

        def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
            calls.append(list(cmd))

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            if callable(side_effect):
                rc = side_effect(cmd)
                if rc is not None:
                    R.returncode = rc
            return R()

        def fake_run_command(cmd, check=True, **kwargs):
            calls.append(list(cmd))

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

        cni_runtime_mock = mock.MagicMock()
        cni_runtime_mock.add_network.return_value = {
            "ips": [{"version": "4", "address": "10.200.0.5/16"}],
        }

        with mock.patch("bandsox.network.subprocess.run", side_effect=fake_run), \
             mock.patch("bandsox.network.run_command", side_effect=fake_run_command), \
             mock.patch("bandsox.cni.CNIRuntime", return_value=cni_runtime_mock), \
             mock.patch("bandsox.network._send_gratuitous_arp"):
            network.setup_netns_networking(
                netns_name=netns_name,
                tap_name=tap_name,
                host_ip=host_ip,
                vm_id=vm_id,
            )

        return calls, netns_name, tap_name

    return run


def test_nft_nat_attempted_before_iptables(captured_netns):
    """When every command succeeds, nft must run first and iptables must NOT run.

    With iptables-legacy and iptables-nft both installed (the Ubuntu 24.04
    default), iptables-legacy returns 0 but the rules are dead. If we tried
    iptables-legacy first and treated success as 'done', NAT would silently
    break. nft must be tried first AND succeed must short-circuit the
    fallbacks.
    """
    calls, ns, tap = captured_netns()

    nft_nat_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) == "nft"
        and "nat" in c
    ]
    assert nft_nat_calls, (
        f"Expected nft 'ip nat' commands inside netns. "
        f"All netns calls: {[c for c in calls if _is_netns_call(c, ns)]}"
    )

    iptables_nat_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) in ("iptables", "iptables-legacy")
        and "nat" in c
    ]
    assert not iptables_nat_calls, (
        f"nft NAT succeeded but iptables NAT was still attempted. "
        f"This means an iptables-legacy success on Ubuntu 24.04 will "
        f"shadow the working nft rule. iptables calls: {iptables_nat_calls}"
    )


def test_nft_mss_clamp_attempted_before_iptables(captured_netns):
    calls, ns, tap = captured_netns()

    nft_mangle_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) == "nft"
        and "mangle" in c
    ]
    assert nft_mangle_calls, (
        f"Expected nft 'ip mangle' commands inside netns for MSS clamp. "
        f"All nft calls inside netns: "
        f"{[c for c in calls if _binary_after_netns(c, ns) == 'nft']}"
    )

    iptables_mangle_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) in ("iptables", "iptables-legacy")
        and "mangle" in c
    ]
    assert not iptables_mangle_calls, (
        f"nft MSS clamp succeeded but iptables MSS clamp was still attempted. "
        f"iptables calls: {iptables_mangle_calls}"
    )


def test_iptables_fallback_runs_when_nft_unavailable(captured_netns):
    """If every nft call fails (no nft binary), we must fall back to iptables.

    This guards the older-Ubuntu / minimal-image case where nft isn't
    installed and we have to rely on iptables-legacy.
    """
    def side_effect(cmd):
        # Make every nft call fail; everything else succeeds.
        if "nft" in cmd:
            return 1
        return 0

    calls, ns, tap = captured_netns(side_effect=side_effect)

    iptables_nat_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) in ("iptables", "iptables-legacy")
        and "nat" in c
    ]
    assert iptables_nat_calls, (
        f"nft failed but iptables fallback was never attempted. "
        f"All netns calls: {[c for c in calls if _is_netns_call(c, ns)]}"
    )

    iptables_mangle_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) in ("iptables", "iptables-legacy")
        and "mangle" in c
    ]
    assert iptables_mangle_calls, (
        f"nft MSS clamp failed but iptables fallback was never attempted. "
        f"All netns calls: {[c for c in calls if _is_netns_call(c, ns)]}"
    )


def test_nft_nat_uses_masquerade_on_eth0(captured_netns):
    """The nft NAT rule must masquerade on the netns-side eth0 (CNI ifname)."""
    calls, ns, tap = captured_netns()

    rule_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) == "nft"
        and "rule" in c
        and "nat" in c
    ]
    assert rule_calls, "No nft 'add rule ip nat' invocation captured."
    rule = rule_calls[0]
    assert "masquerade" in rule, f"nft NAT rule missing 'masquerade': {rule}"
    assert "eth0" in rule, f"nft NAT rule must target eth0: {rule}"


def test_nft_mss_clamp_uses_tap_to_eth0(captured_netns):
    """MSS clamp rule must match TAP -> eth0 SYN packets and clamp to route MTU."""
    calls, ns, tap = captured_netns()

    rule_calls = [
        c for c in calls
        if _binary_after_netns(c, ns) == "nft"
        and "rule" in c
        and "mangle" in c
    ]
    assert rule_calls, "No nft 'add rule ip mangle' invocation captured."
    rule = rule_calls[0]
    assert tap in rule, f"MSS clamp must match iifname={tap}: {rule}"
    assert "eth0" in rule, f"MSS clamp must match oifname=eth0: {rule}"
    assert "syn" in rule, f"MSS clamp must match SYN packets: {rule}"
    # 'set rt mtu' is the nft equivalent of --clamp-mss-to-pmtu.
    assert "rt" in rule and "mtu" in rule, (
        f"MSS clamp must use 'set rt mtu' to clamp to route MTU: {rule}"
    )
