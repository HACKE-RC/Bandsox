import subprocess
import logging
import time

logger = logging.getLogger(__name__)

def run_command(cmd, check=True):
    logger.debug(f"Running command: {' '.join(cmd)}")
    subprocess.run(cmd, check=check)

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
    
    # Check if rule exists to avoid duplication? iptables -C ...
    # For now, we blindly add. In a real app, we should manage chains properly.
    try:
        run_command(["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", ext_if, "-j", "MASQUERADE"])
        run_command(["sudo", "iptables", "-A", "FORWARD", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
        run_command(["sudo", "iptables", "-A", "FORWARD", "-i", tap_name, "-o", ext_if, "-j", "ACCEPT"])
    except subprocess.CalledProcessError as e:
        logger.warning(f"iptables setup failed (might already exist or permission denied): {e}")

def cleanup_tap_device(tap_name: str):
    """Removes a TAP device."""
    logger.info(f"Cleaning up TAP device {tap_name}")
    try:
        run_command(["sudo", "ip", "tuntap", "del", "dev", tap_name, "mode", "tap"], check=False)
        
        # Cleanup iptables? It's hard to remove exactly what we added without tracking.
        # For this prototype, we might leave NAT enabled as it's generally harmless or shared.
        # But ideally we should remove the FORWARD rules for this specific TAP.
        ext_if = get_default_interface()
        run_command(["sudo", "iptables", "-D", "FORWARD", "-i", tap_name, "-o", ext_if, "-j", "ACCEPT"], check=False)
        
    except Exception as e:
        logger.error(f"Error cleaning up TAP device: {e}")
