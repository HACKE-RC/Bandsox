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
    def __init__(self, vm_id: str, socket_path: str, firecracker_bin: str = FIRECRACKER_BIN):
        self.vm_id = vm_id
        self.socket_path = socket_path
        self.console_socket_path = str(Path(socket_path).parent / f"{vm_id}.console.sock")
        self.firecracker_bin = firecracker_bin
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

    def connect_to_console(self):
        """Connects to the console socket if not the owner."""
        if self.process:
            return # We are owner, we use callbacks
            
        if not os.path.exists(self.console_socket_path):
            return # Console socket not ready
            
        self.console_conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.console_conn.connect(self.console_socket_path)
        
        # Start read thread
        t = threading.Thread(target=self._socket_read_loop, daemon=True)
        t.start()
        
        # Check if agent is ready (we might have missed the event)
        # We can try to ping? Or just assume ready if socket exists?
        # Let's assume ready for now, or send a status check?
        self.agent_ready = True 

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
                        cb(data)
                        
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
            # logger.debug(f"VM Output: {line.strip()}")
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

    def start_session(self, command: str, on_stdout=None, on_stderr=None, on_exit=None):
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
            # Better: Use a counter or database.
            # For this task, let's just use a fixed one for the demo or random.
            subnet_idx = int(self.vm_id[-2:], 16) # 0-255
            host_ip = f"172.16.{subnet_idx}.1"
            guest_ip = f"172.16.{subnet_idx}.2"
            guest_mac = f"AA:FC:00:00:{subnet_idx:02x}:02"
            
            setup_tap_device(self.tap_name, host_ip)
            self.network_setup = True
            
            self.client.put_network_interface("eth0", self.tap_name, guest_mac)
            
            # Update boot args to include IP config
            # ip=<client-ip>:<server-ip>:<gw-ip>:<netmask>:<hostname>:<device>:<autoconf>
            # ip=172.16.X.2::172.16.X.1:255.255.255.0::eth0:off
            network_boot_args = f"ip={guest_ip}::{host_ip}:255.255.255.0::eth0:off"
            full_boot_args = f"{boot_args} {network_boot_args}"
            
            # Update boot source with new args
            self.client.put_boot_source(kernel_path, full_boot_args)
        else:
            self.client.put_boot_source(kernel_path, boot_args)

    def start(self):
        """Starts the VM execution."""
        self.client.instance_start()

    def pause(self):
        self.client.pause_vm()

    def resume(self):
        self.client.resume_vm()

    def snapshot(self, snapshot_path: str, mem_file_path: str):
        self.client.create_snapshot(snapshot_path, mem_file_path)

    def load_snapshot(self, snapshot_path: str, mem_file_path: str, enable_networking: bool = True):
        # To load a snapshot, we must start a NEW Firecracker process
        # but NOT configure it (no boot source, no machine config).
        # We just call load_snapshot.
        # However, we DO need to setup network devices if they were present?
        # Firecracker docs say: "The resumed VM will be in the Paused state."
        # And we need to restore the TAP device on the host if it's a new host process/session.
        
        # If we are just restoring into a fresh process:
        # 1. Start process
        # 2. Setup TAP (if not already)
        # 3. Load snapshot
        # 4. Resume
        
        if enable_networking and not self.network_setup:
             # Re-derive IP from ID (assuming same ID)
             subnet_idx = int(self.vm_id[-2:], 16)
             host_ip = f"172.16.{subnet_idx}.1"
             setup_tap_device(self.tap_name, host_ip)
             self.network_setup = True

        self.client.load_snapshot(snapshot_path, mem_file_path)

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
        
        if self.network_setup:
            cleanup_tap_device(self.tap_name)
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
        # debugfs 'cat' command
        # We need to handle binary data? The interface returns str.
        # If the file is binary, this might be an issue.
        # Let's assume text for get_file_contents as per docstring.
        
        # We use subprocess directly here to handle bytes output if needed, 
        # but _run_debugfs handles the pause/resume logic.
        # Let's modify _run_debugfs to return bytes?
        # Or just implement specific logic here.
        
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

    def download_file(self, remote_path: str, local_path: str):
        """Downloads a file from the VM to the local filesystem."""
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
        if not hasattr(self, 'rootfs_path'):
             raise Exception("VM not configured")
             
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

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

    def list_dir(self, path: str) -> list[str]:
        """Lists files in a directory inside the VM."""
        output = self._run_debugfs([f"ls -l {path}"])
        # Parse output
        # debugfs 'ls -l' output format:
        #   inode mode (links) uid gid size date time name
        # Example:
        #     101   40755 (2)      0      0    4096  5-Dec-2025 11:15 etc
        #     534   40755 (2)      0      0    4096  3-Dec-2025 21:18 home
        
        files = []
        import re
        
        # Parse line by line
        for line in output.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            
            # Match format: whitespace, inode, whitespace, mode, whitespace, (links), ..., name at end
            # The name is the last "word" after the time
            # Pattern: skip everything until we get to the timestamp, then capture the rest
            # Format: inode mode (links) uid gid size date time name
            # We need to extract the name which is after the time (HH:MM format)
            
            # Split by whitespace and take the last field (name)
            parts = line.split()
            if len(parts) >= 9:  # Minimum fields: inode mode (links) uid gid size date time name
                name = parts[-1]  # Last field is the filename
                if name not in ['.', '..']:
                    files.append(name)
        
        return files


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

