import logging
import os
import json
import subprocess
import shutil
import random
from pathlib import Path

logger = logging.getLogger(__name__)

class CNIRuntime:
    def __init__(self, netns_path: str):
        self.netns_path = netns_path
        self.netns_name = os.path.basename(netns_path)
        
    def _run_cmd(self, cmd, check=True):
        full_cmd = ["sudo"] + cmd
        res = subprocess.run(full_cmd, capture_output=True, text=True)
        if check and res.returncode != 0:
            raise Exception(f"Command failed: {' '.join(full_cmd)}\nStderr: {res.stderr}")
        return res

    def add_network(self, container_id: str, ifname: str = "eth0"):
        """
        Implements CNI ADD logic for a bridge network.
        """
        bridge_name = "cni-bandsox0"
        subnet = "10.200.0.0/16"
        gateway_ip = "10.200.0.1"
        
        # 1. Ensure Bridge exists on Host
        self._ensure_bridge(bridge_name, gateway_ip)
        
        # 2. Allocate IP for Container
        # Simple IPAM: Hash VM ID to get unique IP in subnet
        # 10.200.X.Y
        # Avoid .0, .1 (gateway), .255?
        import hashlib
        h = int(hashlib.sha256(container_id.encode()).hexdigest(), 16)
        # range 2 to 65534
        idx = (h % 65533) + 2 
        octet3 = idx // 256
        octet4 = idx % 256
        container_ip = f"10.200.{octet3}.{octet4}"
        cidr = "16"
        
        # 3. Create veth pair
        # Host side name
        veth_host = f"veth{container_id[:8]}"
        veth_ns = ifname # inside netns
        
        # Cleanup old if exists
        self._run_cmd(["ip", "link", "del", veth_host], check=False)
        
        # Create
        # ip link add vethHost type veth peer name vethTemp
        veth_temp = f"vtmp{container_id[:4]}"
        self._run_cmd(["ip", "link", "add", veth_host, "type", "veth", "peer", "name", veth_temp])
        
        # 4. Attach Host veth to Bridge
        self._run_cmd(["ip", "link", "set", veth_host, "master", bridge_name])
        self._run_cmd(["ip", "link", "set", veth_host, "up"])
        # Pin the veth MTU to the actual path MTU. This is the ground truth
        # the MSS clamp in network.py needs: --clamp-mss-to-pmtu reads the
        # route MTU, and the route MTU defaults to the device MTU. If we
        # leave both veths at 1500 but the host egress (VPN, overlay, etc.)
        # only carries 1400, the clamp does nothing, the guest advertises
        # MSS=1460, the server sends 1500-byte segments, intermediate
        # routers drop them, and the first HTTPS connection (e.g. git clone)
        # blackholes for ~30s until TCP backs off. 1400 is a safe lower
        # bound that covers WireGuard, most VPNs, GRE, and typical overlays.
        self._run_cmd(["ip", "link", "set", veth_host, "mtu", "1400"])

        # 5. Move peer to NetNS
        self._run_cmd(["ip", "link", "set", veth_temp, "netns", self.netns_name])
        
        # 6. Rename peer inside NetNS
        # ip netns exec <ns> ip link set vethTemp name eth0
        self._run_cmd(["ip", "netns", "exec", self.netns_name, "ip", "link", "set", veth_temp, "name", ifname])
        
        # 7. Configure IP inside NetNS
        self._run_cmd(["ip", "netns", "exec", self.netns_name, "ip", "addr", "add", f"{container_ip}/{cidr}", "dev", ifname])
        self._run_cmd(["ip", "netns", "exec", self.netns_name, "ip", "link", "set", ifname, "up"])
        # Match the host-side MTU so the route MTU inside the netns is 1400
        # and the MSS clamp on the FORWARD chain actually fires.
        self._run_cmd(["ip", "netns", "exec", self.netns_name, "ip", "link", "set", ifname, "mtu", "1400"])
        self._run_cmd(["ip", "netns", "exec", self.netns_name, "ip", "link", "set", "lo", "up"])
        
        # 8. Set Default Route
        # ip netns exec <ns> ip route add default via gateway
        self._run_cmd(["ip", "netns", "exec", self.netns_name, "ip", "route", "add", "default", "via", gateway_ip])
        
        return {
            "cniVersion": "0.4.0",
            "interfaces": [{"name": ifname}],
            "ips": [{"version": "4", "address": f"{container_ip}/{cidr}", "gateway": gateway_ip}]
        }

    def del_network(self, container_id: str, ifname: str = "eth0"):
        """
        Implements CNI DEL logic.
        """
        veth_host = f"veth{container_id[:8]}"
        self._run_cmd(["ip", "link", "del", veth_host], check=False)

    def _ensure_bridge(self, bridge_name, gateway_ip):
        # Check if exists
        res = self._run_cmd(["ip", "link", "show", bridge_name], check=False)
        if res.returncode != 0:
            logger.info(f"Creating bridge {bridge_name}")
            self._run_cmd(["ip", "link", "add", bridge_name, "type", "bridge"])
            self._run_cmd(["ip", "addr", "add", f"{gateway_ip}/16", "dev", bridge_name])
            self._run_cmd(["ip", "link", "set", bridge_name, "up"])
            
        # Ensure IP forwarding is enabled on host (crucial for routing)
        self._run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)
        
        # Ensure Forwarding is allowed for this bridge (default policy may be DROP).
        # Use 'insert' (prepend) rather than 'add' (append): when Docker is
        # running it installs a FORWARD chain with a default DROP at the end,
        # plus a jump to its own DOCKER-USER / DOCKER chains near the top.
        # Appended ACCEPT rules sit after that DROP and never fire, which
        # silently breaks the first packet out of every freshly-restored
        # microVM until conntrack happens to be already populated.
        try:
            self._run_cmd(["sudo", "nft", "insert", "rule", "ip", "filter", "FORWARD", "iifname", bridge_name, "counter", "accept"], check=False)
            self._run_cmd(["sudo", "nft", "insert", "rule", "ip", "filter", "FORWARD", "oifname", bridge_name, "counter", "accept"], check=False)
        except Exception:
            pass
            
        self._ensure_nat()

    def _ensure_nat(self):
        # Enable Masquerade for traffic leaving the Host from this subnet
        # We try nft first (modern, robust), then iptables-legacy, then iptables.
        subnet = "10.200.0.0/16"
        
        # 1. Try nft (if available)
        try:
            # Check if rule exists is hard, so we just add (nft handles idempotency poorly unless we code it perfectly)
            # Or we can check if 'nft' runs.
            subprocess.run(["nft", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Syntax: nft add rule ip nat POSTROUTING ip saddr <subnet> ip daddr != <subnet> counter masquerade
            # We assume 'ip nat POSTROUTING' chain exists (standard). If not, we might fail or need to create it.
            # On this host, we saw it exists.
            # Verify if table/chain exists, or just ensure it.
            # 1. Create table 'ip nat'
            # 2. Create chain 'POSTROUTING'
            # 3. Add rule
            
            subprocess.run(["sudo", "nft", "add", "table", "ip", "nat"], check=True)
            # Create chain with proper hooks (type nat hook postrouting priority 100)
            # We use ignore error if it exists (or rely on 'add' being idempotent-ish for existing hooks?)
            # 'nft add chain' creates if not exists.
            subprocess.run(["sudo", "nft", "add", "chain", "ip", "nat", "POSTROUTING", "{ type nat hook postrouting priority 100; }"], check=True)
            
            # Check if rule exists before adding to avoid duplicates
            # nft list chain ip nat POSTROUTING
            current_rules = subprocess.run(["sudo", "nft", "list", "chain", "ip", "nat", "POSTROUTING"], capture_output=True, text=True).stdout
            
            rule_content = f"ip saddr {subnet} ip daddr != {subnet} counter masquerade"
            
            if rule_content not in current_rules:
                nft_cmd = ["nft", "add", "rule", "ip", "nat", "POSTROUTING"] + rule_content.split()
                subprocess.run(["sudo"] + nft_cmd, check=True)
                
            return
        except Exception:
            # Fallback
            pass

        # Helper to execute iptables
        def try_cmd(base_cmd):
            try:
                # Check
                check_cmd = base_cmd + ["-C", "POSTROUTING", "-s", subnet, "!", "-d", subnet, "-j", "MASQUERADE"]
                cwd_res = subprocess.run(["sudo"] + check_cmd, capture_output=True)
                if cwd_res.returncode == 0:
                    return True # Already exists
                
                # Add
                add_cmd = base_cmd + ["-A", "POSTROUTING", "-s", subnet, "!", "-d", subnet, "-j", "MASQUERADE"]
                subprocess.run(["sudo"] + add_cmd, check=True)
                return True
            except Exception:
                return False

        if try_cmd(["iptables-legacy", "-t", "nat"]):
            return
        if try_cmd(["iptables", "-t", "nat"]):
            return
