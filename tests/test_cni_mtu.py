"""Regression test: CNI veth pair MTU pinning + nft 'insert' (not 'add').

Both pieces guard the same user-visible bug as test_host_mac.py: the first
HTTPS request inside a freshly restored microVM (typically `git clone`)
hangs ~30s and then succeeds on retry.

What this test guards:

1. Both veth ends get MTU pinned to 1400. The MSS clamp in
   network.setup_netns_networking uses --clamp-mss-to-pmtu, which reads
   the route MTU. The route MTU defaults to the device MTU. If we leave
   the veths at 1500 but the host egress only carries 1400 (VPN, GRE,
   typical overlays), the clamp does nothing and the guest still
   advertises MSS=1460 — the inbound TCP segments get blackholed.

2. The nft FORWARD ACCEPT rules are installed with 'insert' (prepend)
   rather than 'add' (append). When Docker is running it owns the
   FORWARD chain with a default DROP near the end. Appended ACCEPT
   rules sit after the DROP and never fire, silently breaking the first
   packet out of every freshly-restored microVM.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.cni import CNIRuntime  # noqa: E402


def _capture_runtime():
    """Returns (runtime, calls) where calls is a list of every cmd dispatched."""
    runtime = CNIRuntime("/var/run/netns/netns_test")
    calls = []

    def fake_run_cmd(cmd, check=True):
        calls.append(list(cmd))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    runtime._run_cmd = fake_run_cmd
    runtime._ensure_bridge = mock.MagicMock()
    return runtime, calls


def test_add_network_pins_host_veth_mtu_to_1400():
    runtime, calls = _capture_runtime()
    runtime.add_network(container_id="abcdef1234567890", ifname="eth0")

    veth_host = "vethabcdef12"
    matching = [
        c for c in calls
        if c[:3] == ["ip", "link", "set"]
        and veth_host in c
        and "mtu" in c
    ]
    assert matching, (
        f"No 'ip link set {veth_host} mtu ...' call. Calls: {calls}"
    )
    assert matching[0][-1] == "1400", (
        f"Host veth MTU should be pinned to 1400, got: {matching[0]}"
    )


def test_add_network_pins_netns_eth0_mtu_to_1400():
    runtime, calls = _capture_runtime()
    runtime.add_network(container_id="abcdef1234567890", ifname="eth0")

    matching = [
        c for c in calls
        if "ip" in c and "netns" in c and "exec" in c
        and "link" in c and "set" in c and "mtu" in c
        and "eth0" in c
    ]
    assert matching, (
        f"No 'ip netns exec ... ip link set eth0 mtu ...' call. Calls: {calls}"
    )
    assert matching[0][-1] == "1400", (
        f"Netns eth0 MTU should be pinned to 1400, got: {matching[0]}"
    )


def test_ensure_bridge_uses_nft_insert_not_add_for_forward_accept():
    """Bridge FORWARD ACCEPT rules must be inserted (prepended), not appended.

    Docker installs a default DROP late in the FORWARD chain; appended
    ACCEPT rules sit after that DROP and never fire.
    """
    runtime = CNIRuntime("/var/run/netns/netns_test")

    nft_calls = []

    def fake_run_cmd(cmd, check=True):
        if "nft" in cmd:
            nft_calls.append(list(cmd))

        class R:
            returncode = 1  # bridge not present, force creation path
            stdout = ""
            stderr = ""

        return R()

    runtime._run_cmd = fake_run_cmd
    runtime._ensure_nat = mock.MagicMock()

    runtime._ensure_bridge("cni-bandsox0", "10.200.0.1")

    forward_rules = [
        c for c in nft_calls
        if "FORWARD" in c and "accept" in c
    ]
    assert forward_rules, f"No nft FORWARD accept rules dispatched. nft calls: {nft_calls}"
    for rule in forward_rules:
        assert "insert" in rule, (
            f"FORWARD ACCEPT rule must use 'nft insert', not 'nft add'. "
            f"Without prepend, Docker's default-DROP swallows our packets "
            f"and the first git clone hangs. Got: {rule}"
        )
        assert "add" not in rule, (
            f"FORWARD ACCEPT rule should not contain 'add' verb: {rule}"
        )
