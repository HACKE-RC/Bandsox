import subprocess
import logging
import time

logger = logging.getLogger(__name__)

def run_command(cmd, check=True):
    logger.debug(f"Running command: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def derive_host_mac(host_ip: str) -> str:
    """Derives a deterministic host-side TAP MAC from the gateway IP.

    Mirrors the guest_mac scheme in vm.py (AA:FC:00:00:{subnet_idx:02x}:02)
    but uses :01 for the host side. Stable across snapshot/restore so the
    guest's cached gateway ARP entry stays valid; without this the TAP gets
    a fresh random MAC on every restore and the first packets the guest
    sends are silently dropped at L2 until ARP ages out (~30-60s). See
    tests/test_host_mac.py.

    Falls back to a hash-based MAC if the IP isn't in the expected
    172.16.X.1 form, so legacy/arbitrary host_ips still get a stable value.
    """
    try:
        parts = host_ip.split(".")
        if len(parts) == 4 and parts[0] == "172" and parts[1] == "16":
            subnet_idx = int(parts[2]) & 0xFF
            return f"AA:FC:00:00:{subnet_idx:02x}:01"
    except Exception:
        pass

    import hashlib

    h = hashlib.sha256(host_ip.encode()).digest()
    # Locally-administered, unicast: set bit 1, clear bit 0 of first octet.
    b0 = (h[0] | 0x02) & 0xFE
    return f"{b0:02x}:{h[1]:02x}:{h[2]:02x}:{h[3]:02x}:{h[4]:02x}:{h[5]:02x}".upper()


def _send_gratuitous_arp(tap_name: str, host_ip: str, netns_name: str = None, count: int = 3):
    """Pushes gratuitous ARPs for host_ip out of tap_name.

    Refreshes the guest's stale gateway ARP entry after a snapshot restore
    so the first guest -> host packet doesn't get dropped at L2. Best-effort:
    arping may not be installed, in which case we silently skip — pinning
    the TAP MAC alone (derive_host_mac) is the primary fix.

    Send several packets (default 3) one second apart so we cover the
    window where the guest's network stack is still coming back up after
    a snapshot resume — a single packet sent before resume gets dropped
    on the floor.
    """
    base = ["sudo"]
    if netns_name:
        base += ["ip", "netns", "exec", netns_name]
    # -A: ARP REPLY (gratuitous announcement). iputils arping rejects
    # fractional intervals, so we just send N packets at the default 1s
    # interval. Total wall time is bounded by -w (count + 1) so a hung
    # arping never wedges restore.
    cmd = base + [
        "arping", "-A", "-c", str(count), "-w", str(count + 1),
        "-I", tap_name, host_ip,
    ]
    try:
        subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=count + 3,
        )
    except Exception as e:
        logger.debug(f"Gratuitous ARP for {host_ip} on {tap_name} skipped: {e}")


def refresh_guest_arp(tap_name: str, host_ip: str, netns_name: str = None):
    """Public helper: send gratuitous ARPs to refresh a resumed guest's
    ARP cache. Safe to call from anywhere on the host (or in a netns).

    This is the post-resume counterpart to the pre-resume ARP sent in
    setup_tap_device / setup_netns_networking. Both are required: the
    pre-resume one covers VMs that boot fresh, the post-resume one
    covers snapshot restores where the guest's stack only comes alive
    after vm.resume().
    """
    _send_gratuitous_arp(tap_name, host_ip, netns_name=netns_name)

def get_default_interface():
    """Get the default network interface with internet access."""
    # Simple heuristic: look for default route
    try:
        result = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
        # Output format: default via 192.168.1.1 dev eth0 proto dhcp ...
        parts = result.stdout.split()
        if "dev" in parts:
            idx = parts.index("dev")
            return parts[idx + 1]
    except Exception as e:
        logger.error(f"Failed to get default interface: {e}")
    return "eth0" # Fallback

def setup_tap_device(tap_name: str, host_ip: str, cidr: int = 24, host_mac: str = None):
    """
    Creates and configures a TAP device.
    
    Args:
        tap_name: Name of the TAP device (e.g., 'tap0')
        host_ip: IP address to assign to the TAP device on the host (gateway for VM)
        cidr: Network mask (e.g., 24)
        host_mac: Optional MAC to pin on the TAP. Derived from host_ip if not given.
            Pinning a stable MAC is required for snapshot restore — otherwise
            the new TAP gets a random MAC and the guest's cached gateway ARP
            entry from snapshot points to the OLD MAC, dropping packets.

    Returns:
        The MAC address that was set on the TAP.
    """
    if not host_mac:
        host_mac = derive_host_mac(host_ip)

    logger.info(f"Setting up TAP device {tap_name} with IP {host_ip}/{cidr} mac {host_mac}")

    # Create TAP device
    # We need to set the user to the current user so Firecracker (running as user) can open it
    import os
    user = os.environ.get("SUDO_USER", os.environ.get("USER", "rc"))
    try:
        run_command(["sudo", "ip", "tuntap", "add", "dev", tap_name, "mode", "tap", "user", user, "group", user])
    except subprocess.CalledProcessError:
        # Ignore if it fails (likely exists). We proceed to set IP/UP which might fix it or fail later.
        logger.warning(f"Failed to create TAP {tap_name} (might already involve). Continuing...")

    # Pin the MAC to the deterministic value. Idempotent — if it's already
    # set to host_mac this is a no-op; if not, this corrects it.
    try:
        run_command(["sudo", "ip", "link", "set", "dev", tap_name, "address", host_mac])
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to set MAC {host_mac} on {tap_name}: {e}")
    
    # Set IP
    # Check for global IP collision
    current_ips_out = subprocess.run(["ip", "-o", "-4", "addr", "list"], capture_output=True, text=True).stdout
    for line in current_ips_out.splitlines():
        if f" {host_ip}/" in line:
            # Line format: 2: eth0    inet 172.16.x.1/24 ...
            parts = line.split()
            dev_name = parts[1]
            if dev_name != tap_name:
                raise Exception(f"IP {host_ip} already assigned to {dev_name}")
            else:
                # Already assigned to this device, skip add
                break
    else:
        # Not found, add it
        try:
            run_command(["sudo", "ip", "addr", "add", f"{host_ip}/{cidr}", "dev", tap_name])
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to assign IP {host_ip} to {tap_name}: {e}")
    
    # Bring up
    run_command(["sudo", "ip", "link", "set", tap_name, "up"])

    # Push a gratuitous ARP so the guest (which may be restoring from a
    # snapshot taken against a previous instance of this TAP) updates its
    # gateway ARP entry immediately.
    _send_gratuitous_arp(tap_name, host_ip)

    # Enable IP forwarding
    run_command(["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"])
    
    # Setup NAT (Masquerading)
    ext_if = get_default_interface()
    logger.info(f"Enabling NAT on interface {ext_if}")
    
    # Check and add firewall rules
    try:
        # Masquerade (NAT)
        run_command(["sudo", "iptables", "-t", "nat", "-C", "POSTROUTING", "-o", ext_if, "-j", "MASQUERADE"], check=False)
        if run_command(["sudo", "iptables", "-t", "nat", "-C", "POSTROUTING", "-o", ext_if, "-j", "MASQUERADE"], check=False).returncode != 0:
             run_command(["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", ext_if, "-j", "MASQUERADE"])
        
        # Conntrack
        if run_command(["sudo", "iptables", "-C", "FORWARD", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"], check=False).returncode != 0:
            run_command(["sudo", "iptables", "-I", "FORWARD", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
            
        # Forward TAP
        if run_command(["sudo", "iptables", "-C", "FORWARD", "-i", tap_name, "-o", ext_if, "-j", "ACCEPT"], check=False).returncode != 0:
            run_command(["sudo", "iptables", "-I", "FORWARD", "-i", tap_name, "-o", ext_if, "-j", "ACCEPT"])
            
        # Allow Host -> VM (and established return traffic)
        # We need to allow packets destined to the TAP device
        if run_command(["sudo", "iptables", "-C", "FORWARD", "-o", tap_name, "-j", "ACCEPT"], check=False).returncode != 0:
             run_command(["sudo", "iptables", "-I", "FORWARD", "-o", tap_name, "-j", "ACCEPT"])

        # Clamp TCP MSS to the path MTU. This prevents a common PMTUD
        # blackhole when the host egress path has MTU < 1500 (e.g. VPN /
        # overlay). Without clamping, the guest advertises MSS=1460 and
        # remote servers may send packets that are too large and get
        # dropped, making the *first* HTTPS request hang until TCP
        # blackhole detection kicks in.
        #
        # We clamp on forwarded SYN packets from TAP -> ext_if.
        mss_check = [
            "sudo", "iptables", "-t", "mangle", "-C", "FORWARD",
            "-i", tap_name, "-o", ext_if,
            "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
            "-j", "TCPMSS", "--clamp-mss-to-pmtu",
        ]
        mss_insert = [
            "sudo", "iptables", "-t", "mangle", "-I", "FORWARD", "1",
            "-i", tap_name, "-o", ext_if,
            "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
            "-j", "TCPMSS", "--clamp-mss-to-pmtu",
        ]
        if run_command(mss_check, check=False).returncode != 0:
            run_command(mss_insert, check=False)
            
    except Exception as e:
        logger.warning(f"iptables setup failed (might already exist or permission denied): {e}")

    return host_mac


def setup_netns_networking(netns_name: str, tap_name: str, host_ip: str, vm_id: str, host_mac: str = None):
    """
    Sets up NetNS using CNI and bridges to a TAP device via TC.

    host_mac, if not provided, is derived from host_ip. Pinning a stable
    MAC matters on snapshot restore (see derive_host_mac docstring).
    """
    import os
    from .cni import CNIRuntime

    user = os.environ.get("SUDO_USER", os.environ.get("USER", "rc"))

    if not host_mac:
        host_mac = derive_host_mac(host_ip)

    logger.info(f"Setting up NetNS {netns_name} using CNI (tap mac {host_mac})")

    # 1. Create NetNS
    # Ensure directory exists for ip netns
    run_command(["sudo", "mkdir", "-p", "/var/run/netns"], check=False)
    run_command(["sudo", "ip", "netns", "add", netns_name])
    
    # 2. Invoke CNI ADD
    # NetNS path for CNI is usually /var/run/netns/<name>
    netns_path = f"/var/run/netns/{netns_name}"
    
    try:
        cni = CNIRuntime(netns_path)
        cni_result = cni.add_network(container_id=vm_id, ifname="eth0")
        logger.info(f"CNI configured eth0: {cni_result}")
    except Exception as e:
        logger.error(f"CNI setup failed: {e}")
        raise e
        
    # 3. Create TAP inside NetNS (for Firecracker)
    # Workaround for "Device or resource busy" if tap_name exists on Host:
    # We create with a temporary name, then rename it to tap_name.
    # This bypasses the collision check that seems to happen with ip tuntap add.
    
    tmp_tap_name = f"{tap_name[:10]}_tmp" # Ensure unique temp name
    
    # Create with temp name
    run_command(["sudo", "ip", "netns", "exec", netns_name, "ip", "tuntap", "add", "dev", tmp_tap_name, "mode", "tap", "user", user, "group", user])
    
    # Rename to target name
    run_command(["sudo", "ip", "netns", "exec", netns_name, "ip", "link", "set", tmp_tap_name, "name", tap_name])

    # Pin the TAP MAC before bringing the link up. Critical on snapshot
    # restore: the guest's preserved ARP cache for host_ip points to the
    # TAP MAC at snapshot time, and a fresh random MAC here would silently
    # drop the first batch of guest -> host packets.
    try:
        run_command(["sudo", "ip", "netns", "exec", netns_name, "ip", "link", "set", "dev", tap_name, "address", host_mac])
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to set MAC {host_mac} on {tap_name} in {netns_name}: {e}")

    # Give TAP the IP expected by the VM (host_ip from snapshot/args)
    run_command(["sudo", "ip", "netns", "exec", netns_name, "ip", "addr", "add", f"{host_ip}/24", "dev", tap_name])
    run_command(["sudo", "ip", "netns", "exec", netns_name, "ip", "link", "set", tap_name, "up"])

    # Belt-and-suspenders: even with the pinned MAC, push a gratuitous ARP
    # so any cache anywhere along the path snaps to the right entry. Also
    # covers legacy snapshots taken before MAC pinning was in place.
    _send_gratuitous_arp(tap_name, host_ip, netns_name=netns_name)
    
    # 4. Enable Forwarding
    run_command(["sudo", "ip", "netns", "exec", netns_name, "sysctl", "-w", "net.ipv4.ip_forward=1"])
    
    # Strategy: Routing + NAT (Double NAT)
    # VM(172.16..) -> TAP -> NAT -> eth0(10.200..) -> CNI Bridge -> Host
    # This ensures packets leaving the NetNS have the CNI-assigned IP.
    
    logger.info("Configuring internal NAT from TAP to eth0 (CNI)")
    def try_netns_iptables(cmd_name):
        try:
            # Check availability first (optional, but good)
            # subprocess.run(["which", cmd_name], check=True, stdout=subprocess.DEVNULL)
            run_command(["sudo", "ip", "netns", "exec", netns_name, cmd_name, "-t", "nat", "-A", "POSTROUTING", "-o", "eth0", "-j", "MASQUERADE"])
            return True
        except Exception:
            return False

    if not try_netns_iptables("iptables-legacy"):
        if not try_netns_iptables("iptables"):
             logger.warning("Failed to setup NAT inside NetNS (tried iptables-legacy and iptables)")

    # Clamp TCP MSS to the path MTU inside the netns. The CNI interface
    # MTU is often smaller than 1500 (e.g. 1400), and snapshot-restored
    # guests frequently keep eth0 at 1500 and advertise MSS=1460. Some
    # networks drop the resulting oversized inbound TCP segments (PMTUD
    # blackhole), which makes the first HTTPS connection (e.g. git clone)
    # hang for ~30s until TCP falls back. MSS clamping fixes this
    # deterministically.
    def try_netns_mss_clamp(cmd_name):
        try:
            run_command(
                [
                    "sudo", "ip", "netns", "exec", netns_name,
                    cmd_name, "-t", "mangle", "-A", "FORWARD",
                    "-i", tap_name, "-o", "eth0",
                    "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
                    "-j", "TCPMSS", "--clamp-mss-to-pmtu",
                ],
                check=False,
            )
            return True
        except Exception:
            return False

    if not try_netns_mss_clamp("iptables-legacy"):
        if not try_netns_mss_clamp("iptables"):
            logger.warning("Failed to setup TCP MSS clamping inside NetNS")

    # Return the CNI assigned IP (IPv4)
    # Result format: {'ips': [{'version': '4', 'address': '10.200.x.x/16', ...}]}
    try:
        if cni_result and "ips" in cni_result:
            for ip_info in cni_result["ips"]:
                if ip_info.get("version") == "4":
                    addr = ip_info.get("address")
                    if addr:
                        return addr.split("/")[0]
    except Exception:
        pass
    return None

def add_host_route(target_subnet: str, gateway_ip: str):
    """Adds a route on the host to the target subnet via gateway."""
    logger.info(f"Adding host route: {target_subnet} via {gateway_ip}")
    try:
        # Use replace to handle existing routes (updates gateway if changed)
        run_command(["sudo", "ip", "route", "replace", target_subnet, "via", gateway_ip])
    except Exception as e:
        logger.warning(f"Failed to add/replace route: {e}")

def delete_host_route(target_subnet: str):
    """Deletes a host route to the target subnet."""
    logger.info(f"Deleting host route: {target_subnet}")
    try:
        run_command(["sudo", "ip", "route", "del", target_subnet], check=False)
    except Exception as e:
        logger.warning(f"Failed to delete route: {e}")



def cleanup_netns(netns_name: str, vm_id: str, host_ip: str):
    """Cleans up the network namespace and CNI resources."""
    logger.info(f"Cleaning up NetNS {netns_name}")
    
    # Use CNI DEL
    try:
        from .cni import CNIRuntime
        cni = CNIRuntime(f"/var/run/netns/{netns_name}")
        cni.del_network(container_id=vm_id, ifname="eth0")
    except Exception as e:
        logger.warning(f"CNI cleanup failed: {e}")

    # Delete NetNS
    try:
        run_command(["sudo", "ip", "netns", "delete", netns_name], check=False)
    except: pass

    
def cleanup_tap_device(tap_name: str, netns_name: str = None, vm_id: str = None, host_ip: str = None):
    """Removes a TAP device or NetNS."""
    if netns_name:
        cleanup_netns(netns_name, vm_id, host_ip)
    else:
        logger.info(f"Cleaning up TAP device {tap_name}")
        try:
            run_command(["sudo", "ip", "tuntap", "del", "dev", tap_name, "mode", "tap"], check=False)
            ext_if = get_default_interface()
            run_command(["sudo", "iptables", "-D", "FORWARD", "-i", tap_name, "-o", ext_if, "-j", "ACCEPT"], check=False)
        except Exception as e:
            logger.error(f"Error cleaning up TAP device: {e}")


def setup_tc_redirect(netns_name: str, src_if: str, dst_if: str):
    """
    Sets up bidirectional traffic mirroring between two interfaces using TC.
    """
    # Ingress on src -> Egress on dst
    run_command(["sudo", "ip", "netns", "exec", netns_name, "tc", "qdisc", "add", "dev", src_if, "ingress"], check=False)
    run_command(["sudo", "ip", "netns", "exec", netns_name, "tc", "filter", "add", "dev", src_if, "parent", "ffff:", "protocol", "all", "u32", "match", "u32", "0", "0", "action", "mirred", "egress", "redirect", "dev", dst_if])

    # Ingress on dst -> Egress on src
    run_command(["sudo", "ip", "netns", "exec", netns_name, "tc", "qdisc", "add", "dev", dst_if, "ingress"], check=False)
    run_command(["sudo", "ip", "netns", "exec", netns_name, "tc", "filter", "add", "dev", dst_if, "parent", "ffff:", "protocol", "all", "u32", "match", "u32", "0", "0", "action", "mirred", "egress", "redirect", "dev", src_if])
    
def configure_tap_offloading(netns_name: str, tap_name: str, vm_id: str):
    """
    Disables checksum offloading.
    """
    # With CNI, we rely on the plugin or disable it manually.
    # We still need to disable on the TAP device inside NetNS.
    try:
        import subprocess
        logger.info(f"Disabling checksum offloading on {tap_name} in {netns_name}")
        run_command(["sudo", "ip", "netns", "exec", netns_name, "ethtool", "-K", tap_name, "tx", "off", "sg", "off", "tso", "off", "ufo", "off", "gso", "off"], check=False)
    except Exception as e:
        logger.warning(f"Failed to run ethtool: {e}")

