import subprocess
import logging
import time

logger = logging.getLogger(__name__)

def run_command(cmd, check=True):
    logger.debug(f"Running command: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)

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

def setup_tap_device(tap_name: str, host_ip: str, cidr: int = 24):
    """
    Creates and configures a TAP device.
    
    Args:
        tap_name: Name of the TAP device (e.g., 'tap0')
        host_ip: IP address to assign to the TAP device on the host (gateway for VM)
        cidr: Network mask (e.g., 24)
    """
    logger.info(f"Setting up TAP device {tap_name} with IP {host_ip}/{cidr}")
    
    # Create TAP device
    # We need to set the user to the current user so Firecracker (running as user) can open it
    import os
    user = os.environ.get("SUDO_USER", os.environ.get("USER", "rc"))
    try:
        run_command(["sudo", "ip", "tuntap", "add", "dev", tap_name, "mode", "tap", "user", user, "group", user])
    except subprocess.CalledProcessError:
        # Ignore if it fails (likely exists). We proceed to set IP/UP which might fix it or fail later.
        logger.warning(f"Failed to create TAP {tap_name} (might already involve). Continuing...")
    
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
            
    except Exception as e:
        logger.warning(f"iptables setup failed (might already exist or permission denied): {e}")


def setup_netns_networking(netns_name: str, tap_name: str, host_ip: str, vm_id: str):
    """
    Sets up NetNS using CNI and bridges to a TAP device via TC.
    """
    import os
    from .cni import CNIRuntime
    
    user = os.environ.get("SUDO_USER", os.environ.get("USER", "rc"))
    
    logger.info(f"Setting up NetNS {netns_name} using CNI")

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
    
    # Give TAP the IP expected by the VM (host_ip from snapshot/args)
    run_command(["sudo", "ip", "netns", "exec", netns_name, "ip", "addr", "add", f"{host_ip}/24", "dev", tap_name])
    run_command(["sudo", "ip", "netns", "exec", netns_name, "ip", "link", "set", tap_name, "up"])
    
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

