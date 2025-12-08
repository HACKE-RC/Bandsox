import subprocess
import os
import logging
import time
import shutil
import uuid
import threading
import json
import socket
from pathlib import Path
from .firecracker import FirecrackerClient
from .network import setup_tap_device, cleanup_tap_device
import requests

logger = logging.getLogger(__name__)

FIRECRACKER_BIN = "/usr/bin/firecracker"
DEFAULT_KERNEL_PATH = "/var/lib/bandsox/vmlinux"
DEFAULT_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off"

class ConsoleMultiplexer:
    def __init__(self, socket_path: str, process: subprocess.Popen):
        self.socket_path = socket_path
        self.process = process
        self.clients = [] # list of client sockets
        self.lock = threading.Lock()
        self.running = True
        self.server_socket = None
        self.callbacks = [] # list of funcs to call with stdout data

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
            
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(self.socket_path)
        self.server_socket.listen(5)
        
        # Thread to accept connections
        t_accept = threading.Thread(target=self._accept_loop, daemon=True)
        t_accept.start()
        
        # Thread to read stdout and broadcast
        t_read = threading.Thread(target=self._read_stdout_loop, daemon=True)
        t_read.start()

    def stop(self):
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def add_callback(self, callback):
        with self.lock:
            self.callbacks.append(callback)

    def write_input(self, data: str):
        """Writes data to the process stdin."""
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except Exception as e:
            logger.error(f"Failed to write to process stdin: {e}")

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.server_socket.accept()
                with self.lock:
                    self.clients.append(client)
                
                # Start thread to read from this client
                t_client = threading.Thread(target=self._client_read_loop, args=(client,), daemon=True)
                t_client.start()
            except Exception:
                if self.running:
                    logger.exception("Error accepting console connection")
                break

    def _read_stdout_loop(self):
        while self.running and self.process.poll() is None:
            line = self.process.stdout.readline()
            if not line:
                break
            
            # Broadcast to callbacks (owner)
            with self.lock:
                for cb in self.callbacks:
                    try:
                        cb(line)
                    except Exception:
                        pass
            
            # Broadcast to clients
            data = line.encode('utf-8')
            with self.lock:
                dead_clients = []
                for client in self.clients:
                    try:
                        client.sendall(data)
                    except Exception:
                        dead_clients.append(client)
                
                for client in dead_clients:
                    self.clients.remove(client)
                    try:
                        client.close()
                    except:
                        pass

    def _client_read_loop(self, client):
        """Reads input from a client and writes to process stdin."""
        try:
            while self.running:
                data = client.recv(4096)
                if not data:
                    break
                # Write to process stdin
                self.write_input(data.decode('utf-8'))
        except Exception:
            pass
        finally:
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
            client.close()


class MicroVM:
    def __init__(self, vm_id: str, socket_path: str, firecracker_bin: str = FIRECRACKER_BIN, netns: str = None):
        self.vm_id = vm_id
        self.socket_path = socket_path
        self.console_socket_path = str(Path(socket_path).parent / f"{vm_id}.console.sock")
        self.firecracker_bin = firecracker_bin
        self.netns = netns
        self.process = None
        self.multiplexer = None
        self.client = FirecrackerClient(socket_path)
        self.tap_name = f"tap{vm_id[:8]}" # Simple TAP naming
        self.network_setup = False
        self.console_conn = None # Connection to console socket if not owner
        self.event_callbacks = {} # cmd_id -> {stdout: func, stderr: func, exit: func}
        self.agent_ready = False

    def start_process(self):
        """Starts the Firecracker process."""
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
            
        cmd = [self.firecracker_bin, "--api-sock", self.socket_path]
        
        # If running in NetNS, wrap command
        if self.netns:
            # We must run as root to enter NetNS, but then drop back to user for Firecracker?
            # Firecracker needs to access KVM (usually group kvm).
            # If we run as root inside NetNS, Firecracker creates socket as root.
            # Client (running as user) cannot connect to root socket easily if permissions derived from umask?
            # Better to run: sudo ip netns exec <ns> sudo -u <user> firecracker ...
            
            # Get current user to switch back to
            user = os.environ.get("SUDO_USER", os.environ.get("USER", "rc"))
            
            # Note: We need full path for sudo if environment is weird, but usually okay.
            cmd = ["sudo", "-n", "ip", "netns", "exec", self.netns, "sudo", "-n", "-u", user] + cmd
            
        logger.info(f"Starting Firecracker: {' '.join(cmd)}")
        # We need pipes for serial console interaction
        self.process = subprocess.Popen(
            cmd, 
            stdin=subprocess.PIPE, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, # Keep stderr separate for logging
            text=True,
            bufsize=1 # Line buffered
        )
        
        # Start Console Multiplexer
        self.multiplexer = ConsoleMultiplexer(self.console_socket_path, self.process)
        self.multiplexer.start()
        
        # Register callback for our own event parsing
        self.multiplexer.add_callback(self._handle_stdout_line)
        
        if not self.client.wait_for_socket():
            raise Exception("Timed out waiting for Firecracker socket")

        # Start thread to read stderr
        t_err = threading.Thread(target=self._read_stderr_loop, daemon=True)
        t_err.start()

    def _read_stderr_loop(self):
        """Reads stderr from the Firecracker process and logs it."""
        while self.process and self.process.poll() is None:
            line = self.process.stderr.readline()
            if line:
                logger.warning(f"VM Stderr: {line.strip()}")
            else:
                break

    def connect_to_console(self):
        """Connects to the console socket if not the owner."""
        if self.process:
            return # We are owner, we use callbacks
            
        if not os.path.exists(self.console_socket_path):
            return # Console socket not ready
            
        self.console_conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.console_conn.connect(self.console_socket_path)
        except (ConnectionRefusedError, FileNotFoundError):
            # This happens if the server restarted and the multiplexer is gone.
            # The VM process might still be running but we can't talk to it.
            logger.error(f"Failed to connect to console socket for {self.vm_id}")
            self.console_conn = None
            raise Exception("VM Agent connection lost. Please restart the VM.")
        
        # Start read thread
        t = threading.Thread(target=self._socket_read_loop, daemon=True)
        t.start()
        
        # Check if agent is ready (we might have missed the event)
        # Do NOT optimistically set ready. Use metadata check in wait_for_agent or send_request.
        # self.agent_ready = True  <-- REMOVED

    def _socket_read_loop(self):
        """Reads from console socket and parses events."""
        buffer = ""
        while True:
            try:
                data = self.console_conn.recv(4096)
                if not data:
                    break
                buffer += data.decode('utf-8')
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    self._handle_stdout_line(line + "\n")
            except Exception:
                break

    def _handle_stdout_line(self, line):
        """Parses a line from stdout (event)."""
        import json
        try:
            event = json.loads(line)
            evt_type = event.get("type")
            payload = event.get("payload")
            
            if evt_type == "status" and payload.get("status") == "ready":
                self.agent_ready = True
                logger.info("Agent is ready")
            
            elif evt_type == "output":
                cmd_id = payload.get("cmd_id")
                stream = payload.get("stream")
                data = payload.get("data")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get(f"on_{stream}")
                    if cb:
                        try:
                             cb(data)
                        except Exception:
                             pass # Don't let callback crash the loop
                        
            elif evt_type == "file_content":
                cmd_id = payload.get("cmd_id")
                content = payload.get("content")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_file_content")
                    if cb:
                        cb(content)

            elif evt_type == "dir_list":
                cmd_id = payload.get("cmd_id")
                files = payload.get("files")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_dir_list")
                    if cb:
                        cb(files)

            elif evt_type == "file_info":
                cmd_id = payload.get("cmd_id")
                info = payload.get("info")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_file_info")
                    if cb:
                        cb(info)

            elif evt_type == "exit":
                cmd_id = payload.get("cmd_id")
                exit_code = payload.get("exit_code")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_exit")
                    if cb:
                        cb(exit_code)
                    # Cleanup
                    del self.event_callbacks[cmd_id]
                    
            elif evt_type == "error":
                cmd_id = payload.get("cmd_id")
                error = payload.get("error")
                logger.error(f"Agent error for cmd {cmd_id}: {error}")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_error")
                    if cb:
                        cb(error)
                
        except json.JSONDecodeError:
            # Log raw output that isn't JSON (kernel logs etc)
            logger.info(f"VM Output: {line.strip()}")
            pass

    def _read_loop(self):
        # Deprecated, logic moved to _handle_stdout_line and multiplexer
        pass

    def send_request(self, req_type: str, payload: dict, on_stdout=None, on_stderr=None, on_file_content=None, on_dir_list=None, on_file_info=None, timeout=30):
        """Sends a JSON request to the agent."""
        if not self.agent_ready:
            # If we are client, try to connect
            if not self.process and not self.console_conn:
                self.connect_to_console()
                
            start = time.time()
            while not self.agent_ready:
                if time.time() - start > 10:
                    raise Exception("Agent not ready")
                time.sleep(0.1)
                
        cmd_id = str(uuid.uuid4())
        payload["id"] = cmd_id
        payload["type"] = req_type
        
        completion_event = threading.Event()
        result = {"code": -1, "error": None}
        
        def on_exit(code):
            result["code"] = code
            completion_event.set()
            
        def on_error(msg):
            result["error"] = msg
            
        self.event_callbacks[cmd_id] = {
            "on_stdout": on_stdout,
            "on_stderr": on_stderr,
            "on_file_content": on_file_content,
            "on_dir_list": on_dir_list,
            "on_file_info": on_file_info,
            "on_exit": on_exit,
            "on_error": on_error
        }
        
        req_str = json.dumps(payload)
        self._write_to_agent(req_str + "\n")
        
        if not completion_event.wait(timeout):
            raise TimeoutError("Command timed out")
            
        if result["error"]:
            raise Exception(f"Agent error: {result['error']}")
            
        return result["code"]

    def _write_to_agent(self, data: str):
        """Writes data to the agent via multiplexer or socket."""
        if self.multiplexer:
            self.multiplexer.write_input(data)
        elif self.console_conn:
            self.console_conn.sendall(data.encode('utf-8'))
        else:
            raise Exception("No connection to agent")

    def exec_command(self, command: str, on_stdout=None, on_stderr=None, timeout=30):
        """Executes a command in the VM via the agent (blocking)."""
        return self.send_request("exec", {"command": command, "background": False}, on_stdout=on_stdout, on_stderr=on_stderr, timeout=timeout)

    def start_session(self, command: str, on_stdout=None, on_stderr=None, on_exit=None) -> str:
        """Starts a background session in the VM."""
        if not self.agent_ready:
             if not self.process and not self.console_conn:
                self.connect_to_console()
             if not self.agent_ready:
                 raise Exception("Agent not ready")
             
        session_id = str(uuid.uuid4())
        
        self.event_callbacks[session_id] = {
            "on_stdout": on_stdout,
            "on_stderr": on_stderr,
            "on_exit": on_exit
        }
        
        req = json.dumps({"type": "exec", "id": session_id, "command": command, "background": True})
        self._write_to_agent(req + "\n")
        
        return session_id

    def start_pty_session(self, command: str, cols: int = 80, rows: int = 24, on_stdout=None, on_exit=None):
        """Starts a PTY session in the VM."""
        if not self.agent_ready:
             if not self.process and not self.console_conn:
                self.connect_to_console()
             if not self.agent_ready:
                 raise Exception("Agent not ready")
             
        session_id = str(uuid.uuid4())
        
        self.event_callbacks[session_id] = {
            "on_stdout": on_stdout, # PTY only has stdout (merged)
            "on_exit": on_exit
        }
        
        req = json.dumps({
            "type": "pty_exec", 
            "id": session_id, 
            "command": command, 
            "cols": cols, 
            "rows": rows
        })
        self._write_to_agent(req + "\n")
        
        return session_id

    def send_session_input(self, session_id: str, data: str, encoding: str = None):
        """Sends input to a session's stdin."""
        if session_id not in self.event_callbacks:
            return

        payload = {"type": "input", "id": session_id, "data": data}
        if encoding:
            payload["encoding"] = encoding
            
        req = json.dumps(payload)
        self._write_to_agent(req + "\n")

    def resize_session(self, session_id: str, cols: int, rows: int):
        """Resizes a PTY session."""
        if session_id not in self.event_callbacks:
            return

        req = json.dumps({
            "type": "resize", 
            "id": session_id, 
            "cols": cols, 
            "rows": rows
        })
        self._write_to_agent(req + "\n")

    def kill_session(self, session_id: str):
        """Kills a session."""
        if session_id not in self.event_callbacks:
            return

        req = json.dumps({"type": "kill", "id": session_id})
        self._write_to_agent(req + "\n")

    def get_guest_ip(self):
        """Returns the guest IP address."""
        if hasattr(self, "network_config") and self.network_config:
            return self.network_config.get("guest_ip")
        
        # Fallback to deterministic calculation
        try:
             subnet_idx = int(self.vm_id[-2:], 16)
             return f"172.16.{subnet_idx}.2"
        except Exception:
             return None

    def send_http_request(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        """
        Sends an HTTP request to the VM.
        args:
            port: Port number
            path: URL path (default: /)
            method: HTTP method (default: GET)
            **kwargs: Arguments passed to requests.request (json, data, headers, timeout, etc.)
        """
        ip = self.get_guest_ip()
        if not ip:
            raise Exception("Could not determine Guest IP (networking might be disabled)")
            
        if not path.startswith("/"):
            path = "/" + path
            
        url = f"http://{ip}:{port}{path}"
        return requests.request(method, url, **kwargs)

    def configure(self, kernel_path: str, rootfs_path: str, vcpu: int, mem_mib: int, boot_args: str = None, enable_networking: bool = True):
        """Configures the VM resources."""
        self.rootfs_path = rootfs_path # Store for file operations
        
        if not boot_args:
            boot_args = f"{DEFAULT_BOOT_ARGS} root=/dev/vda init=/init"

        # 1. Boot Source
        # We set boot source later if networking is enabled to add ip args
        # But if disabled, we set it now or later?
        # Firecracker allows multiple PUTs to boot-source? Yes.
        
        # 2. Rootfs
        self.client.put_drives("rootfs", rootfs_path, is_root_device=True, is_read_only=False)
        
        # 3. Machine Config
        self.client.put_machine_config(vcpu, mem_mib)
        
        # 4. Network
        if enable_networking:
            # We need to set up the TAP device on the host first
            # We'll use a simple IP allocation strategy for this prototype: 
            # 172.16.X.1 (host) <-> 172.16.X.2 (guest)
            # We need a unique X. Let's hash the VM ID or just pick one.
            # For simplicity, let's assume the user manages IP or we pick a random one in 172.16.0.0/16
            # But wait, we need to pass the IP config to the guest via boot args or it needs to use DHCP.
            # Firecracker doesn't provide DHCP. We usually set static IP in guest or use kernel boot args `ip=...`
            
            # Let's use kernel boot args for IP configuration if possible, or assume the rootfs has init script.
            # The user requirement says "Ability to use internet inside the microvm reliably".
            # We'll setup the TAP here.
            
            # Generate a semi-unique subnet based on last byte of VM ID (very naive collision avoidance)
            # Try to allocate a free subnet loop
            base_idx = int(self.vm_id[-2:], 16)
            for i in range(50):
                subnet_idx = (base_idx + i) % 253 + 1 # 1-253
                host_ip = f"172.16.{subnet_idx}.1"
                guest_ip = f"172.16.{subnet_idx}.2"
                guest_mac = f"AA:FC:00:00:{subnet_idx:02x}:02"
                
                try:
                    setup_tap_device(self.tap_name, host_ip)
                    self.network_config = {
                        "host_ip": host_ip,
                        "guest_ip": guest_ip,
                        "guest_mac": guest_mac,
                        "tap_name": self.tap_name
                    }
                    self.network_setup = True
                    logger.info(f"Allocated network {host_ip} for {self.vm_id}")
                    break
                except Exception:
                    # Retry with next subnet
                    continue
            else:
                raise Exception("Failed to allocate free network subnet after retries")
            
            self.client.put_network_interface("eth0", self.tap_name, guest_mac)
            
            self.client.put_network_interface("eth0", self.tap_name, guest_mac)
            
            # Update boot args to include IP config
            # ip=<client-ip>:<server-ip>:<gw-ip>:<netmask>:<hostname>:<device>:<autoconf>:<dns0-ip>
            # ip=172.16.X.2::172.16.X.1:255.255.255.0::eth0:off:8.8.8.8
            network_boot_args = f"ip={guest_ip}::{host_ip}:255.255.255.0::eth0:off:8.8.8.8"
            full_boot_args = f"{boot_args} {network_boot_args}"
            
            # Update boot source with new args
            self.client.put_boot_source(kernel_path, full_boot_args)
        else:
            self.client.put_boot_source(kernel_path, boot_args)

    def update_drive(self, drive_id: str, path_on_host: str):
        """Updates a drive's backing file path."""
        self.client.patch_drive(drive_id, path_on_host)
        if drive_id == "rootfs":
            self.rootfs_path = path_on_host

    def update_network_interface(self, iface_id: str, host_dev_name: str):
        """Updates a network interface's host device."""
        self.client.patch_network_interface(iface_id, host_dev_name)

    def start(self):
        """Starts the VM execution."""
        self.client.instance_start()

    def pause(self):
        self.client.pause_vm()

    def resume(self):
        self.client.resume_vm()

    def snapshot(self, snapshot_path: str, mem_file_path: str):
        self.client.create_snapshot(snapshot_path, mem_file_path)

    def load_snapshot(self, snapshot_path: str, mem_file_path: str, enable_networking: bool = True, guest_mac: str = None):
        # To load a snapshot, we must start a NEW Firecracker process
        # We also need to configure the network backend BEFORE loading the snapshot
        # if the snapshot had a network device.
        
        if enable_networking:
             if not getattr(self, "network_config", None):
                 # Try to allocate a free subnet loop
                 base_idx = int(self.vm_id[-2:], 16)
                 for i in range(50):
                     subnet_idx = (base_idx + i) % 253 + 1
                     host_ip = f"172.16.{subnet_idx}.1"
                     guest_ip = f"172.16.{subnet_idx}.2"
                     current_mac = guest_mac if guest_mac else f"AA:FC:00:00:{subnet_idx:02x}:02"
                     
                     try:
                         setup_tap_device(self.tap_name, host_ip)
                         self.network_config = {
                            "host_ip": host_ip,
                            "guest_ip": guest_ip,
                            "guest_mac": current_mac,
                            "tap_name": self.tap_name
                         }
                         self.network_setup = True
                         break
                     except Exception:
                         continue
                 else:
                     raise Exception("Failed to allocate free network subnet")
                 
             else:
                 # Ensure TAP name is consistent
                 self.network_config["tap_name"] = self.tap_name
                 host_ip = self.network_config["host_ip"]
                 # NOTE: Firecracker restores network config from snapshot if it was configured.
        # We must ensure the TAP device exists with the SAME name as before (handled by core.restore_vm).
        # We do NOT call put_network_interface here because it forbids loading snapshot after config.
        # if enable_networking:
        #    ...
            
        if enable_networking:
            # We rely on the snapshot configuration (pointing to old TAP name).
            # We ensure the device exists in the NetNS via the rename workaround in network.py.
            pass
            
        self.client.load_snapshot(snapshot_path, mem_file_path)

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.network_setup:
            cleanup_tap_device(self.tap_name, netns_name=getattr(self, 'netns', None), vm_id=self.vm_id)
            
            # Cleanup host route if present
            if hasattr(self, 'network_config') and self.network_config and "guest_ip" in self.network_config:
                 from .network import delete_host_route
                 delete_host_route(self.network_config["guest_ip"])
                 
            self.network_setup = False
            
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    @classmethod
    def create_from_snapshot(cls, vm_id: str, snapshot_path: str, mem_file_path: str, socket_path: str, enable_networking: bool = True):
        vm = cls(vm_id, socket_path)
        vm.start_process()
        vm.load_snapshot(snapshot_path, mem_file_path, enable_networking=enable_networking)
        return vm

    def _run_debugfs(self, commands: list[str], write: bool = False):
        """Runs debugfs commands on the rootfs."""
        if not hasattr(self, 'rootfs_path'):
            raise Exception("VM not configured, rootfs_path unknown")

        # Pause VM to prevent corruption if writing or reading inconsistent state
        # We check status first? 
        # For simplicity, always pause/resume if process is running
        was_running = False
        if self.process and self.process.poll() is None:
            # Check if already paused? 
            # We can just call pause(), it's idempotent-ish (Firecracker API might complain if already paused)
            try:
                self.pause()
                was_running = True
            except Exception:
                pass # Maybe already paused or not started fully

        try:
            # Construct debugfs command
            # -w for write access
            cmd = ["debugfs"]
            if write:
                cmd.append("-w")
            
            # Join commands with ; 
            request = "; ".join(commands)
            cmd.extend(["-R", request, self.rootfs_path])
            
            logger.debug(f"Running debugfs: {cmd}")
            result = subprocess.run(cmd, capture_output=True, text=True) # debugfs output might be binary-ish?
            # debugfs 'cat' dumps to stdout. If file is binary, text=True might fail or corrupt.
            # For 'cat', we might need bytes.
            
            if result.returncode != 0:
                raise Exception(f"debugfs failed: {result.stderr}")
                
            return result.stdout
            
        finally:
            if was_running:
                try:
                    self.resume()
                except Exception:
                    pass

    def get_file_contents(self, path: str) -> str:
        """Reads the contents of a file inside the VM."""
        if self.agent_ready:
            result = {}
            def on_file_content(c):
                result["content"] = c
            
            self.send_request("read_file", {"path": path}, on_file_content=on_file_content)
            
            if "content" in result:
                import base64
                return base64.b64decode(result["content"]).decode('utf-8')
            raise Exception(f"Failed to read {path} via agent")

        # debugfs fallback
        if not hasattr(self, 'rootfs_path'):
             raise Exception("VM not configured")

        was_running = False
        if self.process and self.process.poll() is None:
            try:
                self.pause()
                was_running = True
            except Exception:
                pass

        try:
            cmd = ["debugfs", "-R", f"cat {path}", self.rootfs_path]
            # Use bytes for output
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                # debugfs might print error to stderr
                err = result.stderr.decode('utf-8', errors='ignore')
                raise FileNotFoundError(f"Failed to read {path}: {err}")
                
            return result.stdout.decode('utf-8')
        finally:
            if was_running:
                try:
                    self.resume()
                except Exception:
                    pass

    def list_dir(self, path: str) -> list:
        """Lists directory contents."""
        # Try agent first
        if hasattr(self, 'wait_for_agent'):
            try:
                if self.wait_for_agent(timeout=1):
                    result = {}
                    def on_dir_list(files):
                        result["files"] = files
                    
                    self.send_request("list_dir", {"path": path}, on_dir_list=on_dir_list)
                    return result.get("files", [])
            except Exception:
                 pass # Fallback to debugfs

        # Fallback to debugfs
        if not hasattr(self, 'rootfs_path'):
             raise Exception("VM not configured")
             
        # Need to pause for debugfs safety if running
        was_running = False
        if self.process and self.process.poll() is None:
            try:
                self.pause()
                was_running = True
            except Exception:
                pass
                
        try:
            # list directory with debugfs
            cmd = ["debugfs", "-R", f"ls -l {path}", self.rootfs_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                 try:
                     # Retry without -l if it fails? No, simpler ls provides less info.
                     pass
                 except:
                     pass
                 
            # Parse output
            files = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("/"): continue
                
                # Format: inode mode (links) uid gid size date time name
                # Example:   2  40755 (2)      0      0    4096  6-Dec-2025 14:26 .
                parts = line.split()
                if len(parts) >= 8:
                    try:
                        # Depending on debugfs version/output, fields might vary slightly.
                        # Assuming: inode, mode, (links), uid, gid, size, date, time, name
                        # mode is octal usually
                        
                        # Find indices
                        # Mode is 2nd usually
                        mode_oct = parts[1]
                        mode = int(mode_oct, 8)
                        
                        # Size is usually 6th (index 5) if links is split? 
                        # (2) might be one token or split. line.split() handles spaces.
                        # (2) -> "(2)" is one token
                        # so: 0:inode, 1:mode, 2:(links), 3:uid, 4:gid, 5:size, 6:date, 7:time, 8:name
                        
                        size = int(parts[5])
                        name = " ".join(parts[8:]) # Handle spaces in filename?
                        
                        is_dir = (mode & 0o40000) == 0o40000
                        is_file = (mode & 0o100000) == 0o100000
                        
                        if name == '.' or name == '..':
                            continue
                            
                        files.append({
                            "name": name,
                            "type": "directory" if is_dir else "file",
                            "size": size,
                            "mode": mode,
                            "mtime": 0 # TODO: Parse date/time
                        })
                    except (ValueError, IndexError):
                        pass
                        
            return files
            
        finally:
            if was_running:
                try:
                    self.resume()
                except Exception:
                    pass

    def download_file(self, remote_path: str, local_path: str):
        """Downloads a file from the VM to the local filesystem."""
        if self.agent_ready:
            result = {}
            def on_file_content(c):
                result["content"] = c
            
            self.send_request("read_file", {"path": remote_path}, on_file_content=on_file_content)
            
            if "content" in result:
                import base64
                data = base64.b64decode(result["content"])
                
                local_path = os.path.abspath(local_path)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                
                with open(local_path, "wb") as f:
                    f.write(data)
                return
            raise Exception(f"Failed to download {remote_path} via agent")

        if not hasattr(self, 'rootfs_path'):
             raise Exception("VM not configured")

        was_running = False
        if self.process and self.process.poll() is None:
            try:
                self.pause()
                was_running = True
            except Exception:
                pass

        try:
            # debugfs dump command: dump remote_path local_path
            # But local_path must be absolute or relative to cwd?
            # debugfs writes to filesystem directly.
            
            # Ensure local directory exists
            local_dir = os.path.dirname(os.path.abspath(local_path))
            os.makedirs(local_dir, exist_ok=True)
            
            # debugfs 'dump' might not overwrite?
            if os.path.exists(local_path):
                os.unlink(local_path)
                
            cmd = ["debugfs", "-R", f"dump {remote_path} {local_path}", self.rootfs_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                 raise Exception(f"Failed to download {remote_path}: {result.stderr}")
                 
            if not os.path.exists(local_path):
                 raise FileNotFoundError(f"File not downloaded: {remote_path}")
                 
        finally:
            if was_running:
                try:
                    self.resume()
                except Exception:
                    pass

    def upload_file(self, local_path: str, remote_path: str):
        """Uploads a file from local filesystem to the VM."""
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        if self.agent_ready:
            with open(local_path, "rb") as f:
                content = f.read()
            import base64
            encoded = base64.b64encode(content).decode('utf-8')
            
            self.send_request("write_file", {"path": remote_path, "content": encoded})
            return

        if not hasattr(self, 'rootfs_path'):
             raise Exception("VM not configured")
             
        was_running = False
        if self.process and self.process.poll() is None:
            try:
                self.pause()
                was_running = True
            except Exception:
                pass

        try:
            # debugfs write command: write local_path remote_path
            # It creates the file. If it exists, does it overwrite?
            # We might need to rm first.
            
            # Ensure remote directory exists? debugfs mkdir?
            remote_dir = os.path.dirname(remote_path)
            if remote_dir and remote_dir != "/":
                # Recursive mkdir is hard with debugfs.
                # We can try to make it.
                # debugfs doesn't have mkdir -p.
                # We'll assume parent dirs exist or try to create immediate parent.
                # Or we can iterate path components.
                parts = remote_dir.strip("/").split("/")
                current = ""
                for part in parts:
                    current += f"/{part}"
                    subprocess.run(["debugfs", "-w", "-R", f"mkdir {current}", self.rootfs_path], capture_output=True)

            # Remove existing file to ensure overwrite
            subprocess.run(["debugfs", "-w", "-R", f"rm {remote_path}", self.rootfs_path], capture_output=True)
            
            cmd = ["debugfs", "-w", "-R", f"write {local_path} {remote_path}", self.rootfs_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                raise Exception(f"Failed to upload {local_path}: {result.stderr}")
                
        finally:
            if was_running:
                try:
                    self.resume()
                except Exception:
                    pass

    def upload_folder(self, local_path: str, remote_path: str, pattern: str = None, skip_pattern: list[str] = None):
        """
        Uploads a folder recursively.
        """
        import fnmatch
        
        local_path = Path(local_path)
        if not local_path.is_dir():
             raise NotADirectoryError(f"Local path is not a directory: {local_path}")
        
        # We can't easily batch this in one debugfs session without complex logic,
        # so we'll just call upload_file for each file.
        # This will pause/resume for EACH file, which is slow.
        # Optimization: Pause ONCE here, then run raw debugfs commands, then resume.
        
        if not hasattr(self, 'rootfs_path'):
             raise Exception("VM not configured")

        was_running = False
        if self.process and self.process.poll() is None:
            try:
                self.pause()
                was_running = True
            except Exception:
                pass
                
        try:
            # Create remote root dir
            subprocess.run(["debugfs", "-w", "-R", f"mkdir {remote_path}", self.rootfs_path], capture_output=True)
            
            for root, dirs, files in os.walk(local_path):
                rel_root = Path(root).relative_to(local_path)
                remote_root = Path(remote_path) / rel_root
                
                if skip_pattern:
                    for d in list(dirs):
                        if any(fnmatch.fnmatch(d, sp) for sp in skip_pattern):
                            dirs.remove(d)
                
                # Create subdirs
                for d in dirs:
                    r_dir = remote_root / d
                    logger.debug(f"Creating remote dir: {r_dir}")
                    subprocess.run(["debugfs", "-w", "-R", f"mkdir {r_dir}", self.rootfs_path], capture_output=True)
                
                for file in files:
                    if pattern and not fnmatch.fnmatch(file, pattern):
                        continue
                    if skip_pattern and any(fnmatch.fnmatch(file, sp) for sp in skip_pattern):
                        continue
                        
                    local_file_path = str(Path(root) / file)
                    remote_file_path = str(remote_root / file)
                    
                    logger.debug(f"Uploading {local_file_path} to {remote_file_path}")
                    
                    # Remove existing
                    subprocess.run(["debugfs", "-w", "-R", f"rm {remote_file_path}", self.rootfs_path], capture_output=True)
                    
                    # Write
                    cmd = ["debugfs", "-w", "-R", f"write {local_file_path} {remote_file_path}", self.rootfs_path]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode != 0:
                        logger.warning(f"Failed to upload {local_file_path}: {res.stderr}")
                    else:
                        logger.debug(f"Uploaded {local_file_path}")

        finally:
            if was_running:
                try:
                    self.resume()
                except Exception:
                    pass




    def get_file_info(self, path: str) -> dict:
        """Gets file information (size, mtime, etc.) from the VM."""
        # debugfs 'stat' command
        output = self._run_debugfs([f"stat {path}"])
        
        info = {}
        # Parse stat output
        # Inode: 101   Type: directory    Mode:  0755   Flags: 0x80000
        # User:     0   Group:     0   Project:     0   Size: 4096
        # ...
        
        import re
        
        # Parse Type field
        type_match = re.search(r'Type:\s+(\w+)', output)
        if type_match:
            file_type = type_match.group(1).lower()
            info['is_dir'] = file_type == 'directory'
            info['is_file'] = file_type in ['regular', 'file']
        else:
            # Fallback to mode parsing if Type not found
            mode_match = re.search(r'Mode:\s+(\\d+)', output)
            if mode_match:
                mode_oct = int(mode_match.group(1), 8)
                info['mode'] = mode_oct
                info['is_dir'] = (mode_oct & 0o40000) != 0
                info['is_file'] = (mode_oct & 0o100000) != 0
            else:
                info['is_dir'] = False
                info['is_file'] = True
        
        size_match = re.search(r'Size:\s+(\d+)', output)
        if size_match:
            info['size'] = int(size_match.group(1))
            
        # Time parsing
        # mtime: 0x6752c0d5:b34c0000 -- Thu Dec  5 14:35:33 2024
        # Extract the hex timestamp
        
        def parse_time(label):
            m = re.search(f"{label}: 0x([0-9a-fA-F]+)", output)
            if m:
                return int(m.group(1), 16)
            return 0
            
        info['mtime'] = parse_time("mtime")
        info['ctime'] = parse_time("ctime")
        info['atime'] = parse_time("atime")
        
        return info

