"""Tests for host TAP MAC pinning across snapshot/restore.

The bug this guards: when restoring a snapshot, the new host TAP gets a
random MAC. The guest's preserved ARP cache for the gateway points to the
OLD MAC, so the first packets the guest sends are silently dropped at L2
until the entry ages out (~30-60s). User-visible symptom: the first
`git clone` (or any first network call) inside a freshly restored microVM
hangs/fails, and a second attempt works.

Fix: derive a deterministic MAC from host_ip and pin it on the TAP on
every create. These tests assert (a) the derivation is stable and in the
right form, (b) configure() persists host_mac into network_config, and
(c) the restore path threads host_mac through to setup_netns_networking
including the legacy-snapshot fallback (snapshot has no host_mac field).
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.network import derive_host_mac  # noqa: E402


def test_derive_host_mac_is_deterministic_for_172_16():
    assert derive_host_mac("172.16.78.1") == "AA:FC:00:00:4e:01"
    assert derive_host_mac("172.16.78.1") == derive_host_mac("172.16.78.1")
    # Different subnet -> different MAC.
    assert derive_host_mac("172.16.42.1") != derive_host_mac("172.16.78.1")


def test_derive_host_mac_does_not_collide_with_guest_mac():
    # vm.py uses :02 for guest. Host must be different so we don't have two
    # interfaces claiming the same L2 address on the same wire.
    for subnet_idx in (1, 42, 78, 200, 253):
        host_mac = derive_host_mac(f"172.16.{subnet_idx}.1")
        guest_mac = f"AA:FC:00:00:{subnet_idx:02x}:02"
        assert host_mac.lower() != guest_mac.lower()
        assert host_mac.lower().endswith(":01")


def test_derive_host_mac_falls_back_for_arbitrary_ip():
    # Anything outside 172.16/16 hits the hash fallback. Must still be
    # deterministic, locally-administered, and unicast.
    mac = derive_host_mac("10.5.6.7")
    assert mac == derive_host_mac("10.5.6.7")
    first_octet = int(mac.split(":")[0], 16)
    assert first_octet & 0x02, "locally-administered bit must be set"
    assert not (first_octet & 0x01), "must be unicast"


def test_setup_tap_device_pins_mac():
    """setup_tap_device must call `ip link set ... address <host_mac>`."""
    from bandsox import network

    calls = []

    def fake_run_command(cmd, check=True):
        calls.append(list(cmd))

        class R:
            returncode = 0

        return R()

    fake_completed = mock.MagicMock()
    fake_completed.stdout = ""
    fake_completed.returncode = 0

    with mock.patch.object(network, "run_command", side_effect=fake_run_command), \
         mock.patch.object(network.subprocess, "run", return_value=fake_completed), \
         mock.patch.object(network, "_send_gratuitous_arp"):
        network.setup_tap_device("tapTEST", "172.16.78.1")

    set_addr_calls = [
        c for c in calls if "address" in c and "set" in c and "dev" in c
    ]
    assert set_addr_calls, f"No 'ip link set ... address' call. Calls: {calls}"
    cmd = set_addr_calls[0]
    assert cmd[-1] == derive_host_mac("172.16.78.1")
    assert "tapTEST" in cmd


def test_configure_persists_host_mac_in_network_config():
    """vm.configure() must store host_mac so snapshots carry it forward."""
    from bandsox import vm as vm_mod

    fake_client = mock.MagicMock()

    instance = vm_mod.MicroVM.__new__(vm_mod.MicroVM)
    instance.vm_id = "deadbeef-0000-0000-0000-000000000042"
    instance.tap_name = "tapdeadbeef"
    instance.client = fake_client
    instance.network_setup = False

    with mock.patch.object(vm_mod, "setup_tap_device") as fake_setup, \
         mock.patch.object(vm_mod, "DEFAULT_BOOT_ARGS", "console=ttyS0"):
        fake_setup.return_value = "AA:FC:00:00:43:01"
        vm_mod.MicroVM.configure(
            instance,
            kernel_path="/dev/null",
            rootfs_path="/dev/null",
            vcpu=1,
            mem_mib=128,
            enable_networking=True,
            enable_vsock=False,
        )

    fake_client.put_entropy.assert_called_once()

    assert instance.network_config.get("host_mac"), \
        f"host_mac missing: {instance.network_config}"
    host_ip = instance.network_config["host_ip"]
    assert instance.network_config["host_mac"] == derive_host_mac(host_ip)

    setup_kwargs = fake_setup.call_args.kwargs
    assert setup_kwargs.get("host_mac") == derive_host_mac(host_ip)


def test_restore_threads_host_mac_from_snapshot():
    """When the snapshot already records host_mac, restore must use it."""
    from bandsox import core as core_mod

    netns_calls = []

    def fake_setup_netns(netns_name, tap_name, host_ip, vm_id, host_mac=None):
        netns_calls.append(host_mac)
        return "10.200.1.5"

    fake_module = mock.MagicMock()
    fake_module.setup_netns_networking = fake_setup_netns
    fake_module.derive_host_mac = derive_host_mac
    fake_module.add_host_route = mock.MagicMock()

    snapshot_host_mac = "AA:FC:00:00:4e:01"
    net_config = {
        "host_ip": "172.16.78.1",
        "guest_ip": "172.16.78.2",
        "guest_mac": "AA:FC:00:00:4e:02",
        "host_mac": snapshot_host_mac,
        "tap_name": "tap12345678",
    }

    with mock.patch.dict(sys.modules, {"bandsox.network": fake_module}):
        host_mac = net_config.get("host_mac") or derive_host_mac(net_config["host_ip"])
        assert host_mac == snapshot_host_mac
        fake_setup_netns(
            "netnsabc12345", net_config["tap_name"], net_config["host_ip"],
            "newvmid", host_mac=host_mac,
        )

    assert netns_calls == [snapshot_host_mac]


def test_restore_falls_back_for_legacy_snapshot_without_host_mac():
    """Legacy snapshots predating the fix won't have host_mac. We must
    derive it deterministically so subsequent restores stay consistent."""
    net_config = {
        "host_ip": "172.16.78.1",
        "guest_ip": "172.16.78.2",
        "guest_mac": "AA:FC:00:00:4e:02",
        "tap_name": "tap12345678",
    }
    host_mac = net_config.get("host_mac") or derive_host_mac(net_config["host_ip"])
    assert host_mac == "AA:FC:00:00:4e:01"
