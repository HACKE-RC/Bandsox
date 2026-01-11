import subprocess
import os
import logging
import time
import shutil
import uuid
import threading
import json
import socket
import errno
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
        self.clients = []  # list of client sockets
        self.lock = threading.Lock()
        self.running = True
        self.server_socket = None
        self.callbacks = []  # list of funcs to call with stdout data

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
                t_client = threading.Thread(
                    target=self._client_read_loop, args=(client,), daemon=True
                )
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
            data = line.encode("utf-8")
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
                self.write_input(data.decode("utf-8"))
        except Exception:
            pass
        finally:
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
            client.close()


class MicroVM:
    def __init__(
        self,
        vm_id: str,
        socket_path: str,
        firecracker_bin: str = FIRECRACKER_BIN,
        netns: str = None,
    ):
        self.vm_id = vm_id
        self.socket_path = socket_path
        self.console_socket_path = str(
            Path(socket_path).parent / f"{vm_id}.console.sock"
        )
        self.firecracker_bin = firecracker_bin
        self.netns = netns
        self.process = None
        self.multiplexer = None
        self.client = FirecrackerClient(socket_path)
        self.tap_name = f"tap{vm_id[:8]}"  # Simple TAP naming
        self.network_setup = False
        self.console_conn = None  # Connection to console socket if not owner
        self.event_callbacks = {}  # cmd_id -> {stdout: func, stderr: func, exit: func}
        self.agent_ready = False
        self.env_vars = {}
        self._uv_available = None  # Cache for uv availability check

        self.vsock_enabled = False
        self.vsock_cid = None
        self.vsock_port = None
        self.vsock_socket_path = None
        self.vsock_listener = None  # New: VsockHostListener instance
        # Legacy bridge vars (to be removed, kept for compatibility during transition)
        self.vsock_bridge_socket = None
        self.vsock_bridge_thread = None
        self.vsock_bridge_running = False

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
            cmd = ["sudo", "ip", "netns", "exec", self.netns, "sudo", "-u", user] + cmd

        logger.info(f"Starting Firecracker: {' '.join(cmd)}")
        # We need pipes for serial console interaction
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # Keep stderr separate for logging
            text=True,
            bufsize=1,  # Line buffered
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
            return  # We are owner, we use callbacks

        if not os.path.exists(self.console_socket_path):
            return  # Console socket not ready

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
                buffer += data.decode("utf-8")
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

            if evt_type == "status":
                status = payload.get("status")
                if status == "ready":
                    self.agent_ready = True
                    logger.info("Agent is ready")
                elif status == "started":
                    cmd_id = payload.get("cmd_id")
                    pid = payload.get("pid")
                    if cmd_id in self.event_callbacks:
                        cb = self.event_callbacks[cmd_id].get("on_started")
                        if cb:
                            cb(pid)

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
                            pass  # Don't let callback crash the loop

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

            elif evt_type == "file_chunk":
                cmd_id = payload.get("cmd_id")
                data = payload.get("data")
                offset = payload.get("offset")
                size = payload.get("size")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_file_chunk")
                    if cb:
                        cb(data, offset, size)

            elif evt_type == "file_complete":
                cmd_id = payload.get("cmd_id")
                total_size = payload.get("total_size")
                checksum = payload.get("checksum")
                if cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_file_complete")
                    if cb:
                        cb(total_size, checksum)

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

    def send_request(
        self,
        req_type: str,
        payload: dict,
        on_stdout=None,
        on_stderr=None,
        on_file_content=None,
        on_file_chunk=None,
        on_file_complete=None,
        on_dir_list=None,
        on_file_info=None,
        timeout=30,
    ):
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
            "on_file_chunk": on_file_chunk,
            "on_file_complete": on_file_complete,
            "on_dir_list": on_dir_list,
            "on_file_info": on_file_info,
            "on_exit": on_exit,
            "on_error": on_error,
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
            self.console_conn.sendall(data.encode("utf-8"))
        else:
            raise Exception("No connection to agent")

    def exec_command(self, command: str, on_stdout=None, on_stderr=None, timeout=30):
        """Executes a command in the VM via the agent (blocking)."""
        return self.send_request(
            "exec",
            {"command": command, "background": False, "env": self.env_vars},
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            timeout=timeout,
        )

    def exec_python(
        self,
        code: str,
        cwd: str = "/tmp",
        packages: list = None,
        on_stdout=None,
        on_stderr=None,
        timeout=60,
        cleanup_venv: bool = True,
    ):
        """
        Executes Python code in the VM with isolated dependencies.

        This function never raises exceptions - all errors are returned via stderr callback
        and a non-zero exit code.

        Args:
            code: Python code to execute
            cwd: Working directory to execute code in (default: /tmp)
            packages: List of Python packages to install via uv before execution
            on_stdout: Callback for stdout output
            on_stderr: Callback for stderr output
            timeout: Timeout in seconds (default: 60)
            cleanup_venv: Whether to clean up the venv after execution (default: True)

        Returns:
            Exit code (0 for success, 1 for error)
        """
        import base64
        import traceback

        # Generate unique names for temp files
        unique_id = uuid.uuid4().hex[:8]
        temp_script = f"/tmp/exec_python_{unique_id}.py"
        venv_dir = f"/tmp/venv_{unique_id}"

        def send_error(msg):
            """Send error message to stderr callback"""
            if on_stderr:
                try:
                    on_stderr(f"ERROR: {msg}\n")
                except:
                    pass

        try:
            # Write Python code to a temporary file in the VM
            # Encode code as base64 to handle special characters
            try:
                encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
                write_cmd = f'echo "{encoded_code}" | base64 -d > {temp_script}'
                exit_code = self.exec_command(write_cmd, timeout=timeout)
                if exit_code != 0:
                    send_error(
                        f"Failed to write Python script to VM (exit code: {exit_code})"
                    )
                    return 1
            except Exception as e:
                send_error(f"Failed to prepare script: {e}")
                return 1

            # Check if uv is available, if not, try to install it or use standard venv
            try:
                if self._uv_available is None:
                    uv_check = self.exec_command("which uv", timeout=5)
                    self._uv_available = uv_check == 0

                    if not self._uv_available:
                        # Try to install uv
                        logger.info("uv not found, attempting to install it...")
                        install_uv_cmd = (
                            "curl -LsSf https://astral.sh/uv/install.sh | sh"
                        )
                        uv_install_exit = self.exec_command(install_uv_cmd, timeout=60)

                        if uv_install_exit == 0:
                            # Check if uv is now in PATH (it might be in ~/.cargo/bin)
                            uv_check2 = self.exec_command(
                                "which uv || test -f ~/.cargo/bin/uv", timeout=5
                            )
                            self._uv_available = uv_check2 == 0
                            if self._uv_available:
                                logger.info("uv installed successfully")

                use_uv = self._uv_available
            except Exception as e:
                logger.warning(f"Error checking uv: {e}")
                use_uv = False

            # If no packages needed, use system Python directly (faster, no venv overhead)
            if not packages:
                exec_cmd = f"cd {cwd} && python3 {temp_script}"
                return self.exec_command(
                    exec_cmd, on_stdout=on_stdout, on_stderr=on_stderr, timeout=timeout
                )

            # Create a separate venv for this execution
            try:
                if use_uv:
                    # Use uv if available (check if it's in PATH or ~/.cargo/bin)
                    venv_cmd = (
                        f"(uv venv {venv_dir} || ~/.cargo/bin/uv venv {venv_dir})"
                    )
                else:
                    # Fall back to standard Python venv
                    logger.info("Using standard Python venv (uv not available)")
                    venv_cmd = f"python3 -m venv {venv_dir}"

                venv_exit = self.exec_command(
                    venv_cmd, on_stdout=on_stdout, on_stderr=on_stderr, timeout=timeout
                )
                if venv_exit != 0:
                    send_error(f"Failed to create venv (exit code: {venv_exit})")
                    return 1
            except Exception as e:
                send_error(f"Failed to create venv: {e}")
                return 1

            # Install packages if provided
            if packages and len(packages) > 0:
                try:
                    packages_str = " ".join(packages)

                    if use_uv:
                        # Install packages using uv in the isolated venv
                        install_cmd = f"(uv pip install --python {venv_dir}/bin/python {packages_str} || ~/.cargo/bin/uv pip install --python {venv_dir}/bin/python {packages_str})"
                    else:
                        # Use pip from the venv
                        install_cmd = f"{venv_dir}/bin/pip install {packages_str}"

                    install_exit = self.exec_command(
                        install_cmd,
                        on_stdout=on_stdout,
                        on_stderr=on_stderr,
                        timeout=timeout,
                    )
                    if install_exit != 0:
                        logger.warning(
                            f"Package installation failed with exit code {install_exit}"
                        )
                        # Continue anyway - the script might still work
                except Exception as e:
                    logger.warning(f"Error installing packages: {e}")
                    # Continue anyway

            # Execute the Python script in the venv and specified working directory
            try:
                exec_cmd = f"cd {cwd} && {venv_dir}/bin/python {temp_script}"
                return self.exec_command(
                    exec_cmd, on_stdout=on_stdout, on_stderr=on_stderr, timeout=timeout
                )
            except Exception as e:
                send_error(f"Failed to execute Python script: {e}")
                return 1

        except Exception as e:
            # Catch any unexpected errors
            send_error(
                f"Unexpected error in exec_python: {e}\n{traceback.format_exc()}"
            )
            return 1

        finally:
            # Clean up the temporary script file and venv
            try:
                self.exec_command(f"rm -f {temp_script}", timeout=5)
                if cleanup_venv:
                    self.exec_command(f"rm -rf {venv_dir}", timeout=10)
            except Exception as e:
                logger.warning(f"Failed to clean up temporary files: {e}")

    def exec_python_capture(
        self,
        code: str,
        cwd: str = "/tmp",
        packages: list = None,
        timeout=60,
        cleanup_venv: bool = True,
    ):
        """
        Executes Python code and captures the output.

        This is a convenience wrapper around exec_python that automatically captures
        stdout and stderr and returns them along with the exit code.

        This function never raises exceptions - all errors are captured and returned
        in the result dictionary.

        Args:
            code: Python code to execute
            cwd: Working directory to execute code in (default: /tmp)
            packages: List of Python packages to install via uv before execution
            timeout: Timeout in seconds (default: 60)
            cleanup_venv: Whether to clean up the venv after execution (default: True)

        Returns:
            dict with keys:
                - 'exit_code': int (0 for success, 1+ for error)
                - 'stdout': str (combined stdout)
                - 'stderr': str (combined stderr)
                - 'output': str (combined stdout + stderr in order)
                - 'success': bool (True if exit_code == 0)
                - 'error': str or None (error message if failed, None if success)
        """
        import traceback

        stdout_lines = []
        stderr_lines = []
        all_output = []

        def capture_stdout(line):
            stdout_lines.append(line)
            all_output.append(("stdout", line))

        def capture_stderr(line):
            stderr_lines.append(line)
            all_output.append(("stderr", line))

        try:
            exit_code = self.exec_python(
                code=code,
                cwd=cwd,
                packages=packages,
                on_stdout=capture_stdout,
                on_stderr=capture_stderr,
                timeout=timeout,
                cleanup_venv=cleanup_venv,
            )

            stdout_str = "".join(stdout_lines)
            stderr_str = "".join(stderr_lines)
            output_str = "".join(line for _, line in all_output)

            return {
                "exit_code": exit_code,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "output": output_str,
                "success": exit_code == 0,
                "error": stderr_str if exit_code != 0 else None,
            }

        except Exception as e:
            # If exec_python somehow raises (it shouldn't), catch it here
            error_msg = f"Unexpected error in exec_python_capture: {e}\n{traceback.format_exc()}"
            return {
                "exit_code": 1,
                "stdout": "".join(stdout_lines),
                "stderr": error_msg,
                "output": "".join(line for _, line in all_output) + error_msg,
                "success": False,
                "error": error_msg,
            }

    def start_session(
        self, command: str, on_stdout=None, on_stderr=None, on_exit=None
    ) -> tuple[str, int | None]:
        """Starts a background session in the VM.

        Returns:
            tuple: (session_id, pid) where pid is the process ID of the started command,
                   or None if the PID could not be retrieved within 5 seconds.
        """
        if not self.agent_ready:
            if not self.process and not self.console_conn:
                self.connect_to_console()
            if not self.agent_ready:
                raise Exception("Agent not ready")

        session_id = str(uuid.uuid4())

        # Event to signal when we receive the started status with PID
        started_event = threading.Event()
        pid_result = {"pid": None}

        def on_started(pid):
            pid_result["pid"] = pid
            started_event.set()

        self.event_callbacks[session_id] = {
            "on_stdout": on_stdout,
            "on_stderr": on_stderr,
            "on_exit": on_exit,
            "on_started": on_started,
        }

        req = json.dumps(
            {
                "type": "exec",
                "id": session_id,
                "command": command,
                "background": True,
                "env": self.env_vars,
            }
        )
        self._write_to_agent(req + "\n")

        # Wait for the started event with PID (max 5 seconds)
        started_event.wait(timeout=5)

        return (session_id, pid_result["pid"])

    def start_pty_session(
        self, command: str, cols: int = 80, rows: int = 24, on_stdout=None, on_exit=None
    ):
        """Starts a PTY session in the VM."""
        if not self.agent_ready:
            if not self.process and not self.console_conn:
                self.connect_to_console()
            if not self.agent_ready:
                raise Exception("Agent not ready")

        session_id = str(uuid.uuid4())

        self.event_callbacks[session_id] = {
            "on_stdout": on_stdout,  # PTY only has stdout (merged)
            "on_exit": on_exit,
        }

        req = json.dumps(
            {
                "type": "pty_exec",
                "id": session_id,
                "command": command,
                "cols": cols,
                "rows": rows,
            }
        )
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

        req = json.dumps(
            {"type": "resize", "id": session_id, "cols": cols, "rows": rows}
        )
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

    def send_http_request(
        self, port: int, path: str = "/", method: str = "GET", **kwargs
    ):
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
            raise Exception(
                "Could not determine Guest IP (networking might be disabled)"
            )

        if not path.startswith("/"):
            path = "/" + path

        url = f"http://{ip}:{port}{path}"
        return requests.request(method, url, **kwargs)

    def configure(
        self,
        kernel_path: str,
        rootfs_path: str,
        vcpu: int,
        mem_mib: int,
        boot_args: str = None,
        enable_networking: bool = True,
        enable_vsock: bool = True,
    ):
        """Configures the VM resources."""
        self.rootfs_path = rootfs_path

        if not boot_args:
            boot_args = f"{DEFAULT_BOOT_ARGS} root=/dev/vda init=/init"

        self.client.put_drives(
            "rootfs", rootfs_path, is_root_device=True, is_read_only=False
        )

        self.client.put_machine_config(vcpu, mem_mib)

        if enable_networking:
            base_idx = int(self.vm_id[-2:], 16)
            for i in range(50):
                subnet_idx = (base_idx + i) % 253 + 1
                host_ip = f"172.16.{subnet_idx}.1"
                guest_ip = f"172.16.{subnet_idx}.2"
                guest_mac = f"AA:FC:00:00:{subnet_idx:02x}:02"

                try:
                    setup_tap_device(self.tap_name, host_ip)
                    self.network_config = {
                        "host_ip": host_ip,
                        "guest_ip": guest_ip,
                        "guest_mac": guest_mac,
                        "tap_name": self.tap_name,
                    }
                    self.network_setup = True
                    logger.info(f"Allocated network {host_ip} for {self.vm_id}")
                    break
                except Exception:
                    continue
            else:
                raise Exception("Failed to allocate free network subnet after retries")

            self.client.put_network_interface("eth0", self.tap_name, guest_mac)

            network_boot_args = (
                f"ip={guest_ip}::{host_ip}:255.255.255.0::eth0:off:8.8.8.8"
            )
            full_boot_args = f"{boot_args} {network_boot_args}"

            self.client.put_boot_source(kernel_path, full_boot_args)
        else:
            self.client.put_boot_source(kernel_path, boot_args)

        if enable_vsock:
            from .core import BandSox

            bs = BandSox()
            cid = bs._allocate_cid()
            port = bs._allocate_port()
            self._setup_vsock_bridge(cid, port)

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

    def load_snapshot(
        self,
        snapshot_path: str,
        mem_file_path: str,
        enable_networking: bool = True,
        guest_mac: str = None,
    ):
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
                    current_mac = (
                        guest_mac if guest_mac else f"AA:FC:00:00:{subnet_idx:02x}:02"
                    )

                    try:
                        setup_tap_device(self.tap_name, host_ip)
                        self.network_config = {
                            "host_ip": host_ip,
                            "guest_ip": guest_ip,
                            "guest_mac": current_mac,
                            "tap_name": self.tap_name,
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

        self._cleanup_vsock_bridge()

        should_cleanup_net = (
            self.network_setup
            or getattr(self, "netns", None)
            or getattr(self, "network_config", None)
        )
        if should_cleanup_net:
            cleanup_tap_device(
                self.tap_name, netns_name=getattr(self, "netns", None), vm_id=self.vm_id
            )

            # Cleanup host route if present
            if (
                hasattr(self, "network_config")
                and self.network_config
                and "guest_ip" in self.network_config
            ):
                from .network import delete_host_route

                delete_host_route(self.network_config["guest_ip"])

            self.network_setup = False

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def _setup_vsock_bridge(self, cid: int, port: int):
        """Sets up vsock for high-speed file transfers using guest-initiated connections.

        This method:
        1. Configures Firecracker to create a vsock device with uds_path
        2. Starts a VsockHostListener on uds_path_PORT for guest connections

        The guest connects to us via AF_VSOCK(CID=2, port), and Firecracker
        routes the connection to our listener socket at uds_path_PORT.

        Args:
            cid: Guest Context ID (for Firecracker config)
            port: Port number for guest to connect to
        """
        from .vsock import VsockHostListener

        self.vsock_socket_path = f"/tmp/bandsox/vsock_{self.vm_id}.sock"

        # Pre-cleanup: Remove any stale socket files
        if os.path.exists(self.vsock_socket_path):
            try:
                os.unlink(self.vsock_socket_path)
                logger.debug(f"Removed stale vsock socket: {self.vsock_socket_path}")
            except Exception as e:
                logger.warning(
                    f"Failed to remove stale socket {self.vsock_socket_path}: {e}"
                )

        # Also clean up listener socket if it exists
        listener_path = f"{self.vsock_socket_path}_{port}"
        if os.path.exists(listener_path):
            try:
                os.unlink(listener_path)
                logger.debug(f"Removed stale listener socket: {listener_path}")
            except Exception as e:
                logger.warning(
                    f"Failed to remove stale listener socket {listener_path}: {e}"
                )

        try:
            # Tell Firecracker to create vsock device
            # Firecracker creates a Unix socket at uds_path for host-initiated connections
            # For guest-initiated connections, it routes to uds_path_PORT
            logger.debug(
                f"Configuring Firecracker vsock: CID={cid}, socket={self.vsock_socket_path}"
            )
            self.client.put_vsock("vsock0", cid, self.vsock_socket_path)

            # Create and start our listener for guest-initiated connections
            # Guest connects to AF_VSOCK(CID=2, port) -> Firecracker routes to our listener
            self.vsock_listener = VsockHostListener(
                uds_path=self.vsock_socket_path,
                port=port,
            )
            self.vsock_listener.start()

            # Set these AFTER successful setup
            self.vsock_enabled = True
            self.vsock_cid = cid
            self.vsock_port = port

            self.env_vars["BANDSOX_VSOCK_PORT"] = str(port)
            logger.info(
                f"Vsock enabled: CID={cid}, port={port}, listener={listener_path}"
            )

        except Exception as e:
            logger.error(f"Failed to setup vsock: {e}")
            self._cleanup_vsock_bridge()
            raise Exception(f"Failed to setup vsock: {e}") from e

    def _cleanup_vsock_bridge(self):
        """Cleans up vsock resources."""
        logger.debug(f"Cleaning up vsock for {self.vm_id}")

        # Stop the listener
        if self.vsock_listener:
            try:
                self.vsock_listener.stop()
                logger.debug("Vsock listener stopped")
            except Exception as e:
                logger.debug(f"Error stopping vsock listener: {e}")
            self.vsock_listener = None

        # Legacy cleanup for bridge (if any)
        self.vsock_bridge_running = False
        if self.vsock_bridge_socket:
            try:
                self.vsock_bridge_socket.close()
            except Exception:
                pass
            self.vsock_bridge_socket = None
        if self.vsock_bridge_thread and self.vsock_bridge_thread.is_alive():
            try:
                self.vsock_bridge_thread.join(timeout=1)
            except Exception:
                pass
            self.vsock_bridge_thread = None

        # Don't remove the main socket file - Firecracker owns it
        # Only clean up if we're sure we created it (e.g., on failure before VM start)

        self.vsock_socket_path = None
        self.vsock_enabled = False
        self.vsock_cid = None
        self.vsock_port = None

        if "BANDSOX_VSOCK_PORT" in self.env_vars:
            del self.env_vars["BANDSOX_VSOCK_PORT"]

    def setup_vsock_listener(self, port: int = None):
        """Set up vsock listener for an already-running VM (e.g., after restore).

        Call this after restore to enable vsock file transfers.
        The Firecracker vsock device must already be configured.

        Args:
            port: Port to listen on (uses self.vsock_port if not specified)
        """
        from .vsock import VsockHostListener

        if port is None:
            port = self.vsock_port
        if port is None:
            raise ValueError("No vsock port specified")

        if not self.vsock_socket_path:
            raise ValueError("No vsock socket path configured")

        # Create and start listener
        listener_path = f"{self.vsock_socket_path}_{port}"

        # Clean up stale listener socket
        if os.path.exists(listener_path):
            try:
                os.unlink(listener_path)
            except Exception:
                pass

        self.vsock_listener = VsockHostListener(
            uds_path=self.vsock_socket_path,
            port=port,
        )
        self.vsock_listener.start()

        self.vsock_enabled = True
        self.env_vars["BANDSOX_VSOCK_PORT"] = str(port)
        logger.info(f"Vsock listener started: port={port}, path={listener_path}")

    @classmethod
    def create_from_snapshot(
        cls,
        vm_id: str,
        snapshot_path: str,
        mem_file_path: str,
        socket_path: str,
        enable_networking: bool = True,
    ):
        vm = cls(vm_id, socket_path)
        vm.start_process()
        vm.load_snapshot(
            snapshot_path, mem_file_path, enable_networking=enable_networking
        )
        return vm

    def get_file_contents(self, path: str) -> str:
        """Reads the contents of a file inside the VM."""
        if not self.agent_ready:
            raise Exception("Agent not ready")

        result = {}

        def on_file_content(c):
            result["content"] = c

        self.send_request("read_file", {"path": path}, on_file_content=on_file_content)

        if "content" in result:
            import base64

            return base64.b64decode(result["content"]).decode("utf-8")
        raise Exception(f"Failed to read {path} via agent")

    def list_dir(self, path: str) -> list:
        """Lists directory contents."""
        if not self.agent_ready:
            raise Exception("Agent not ready")

        result = {}

        def on_dir_list(files):
            result["files"] = files

        self.send_request("list_dir", {"path": path}, on_dir_list=on_dir_list)
        return result.get("files", [])

    def download_file(self, remote_path: str, local_path: str, timeout: int = 300):
        """Downloads a file from the VM to the local filesystem.

        Handles both small files (single file_content event) and large files
        (chunked via file_chunk/file_complete events).

        Args:
            remote_path: Path to file in VM
            local_path: Path to save file locally
            timeout: Timeout in seconds (default 300 for large files over serial)
        """
        if not self.agent_ready:
            raise Exception("Agent not ready")

        import base64
        import hashlib

        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        result = {
            "mode": None,
            "content": None,
            "file_handle": None,
            "md5": None,
            "error": None,
        }

        def on_file_content(content):
            """Handle small file (single shot transfer)."""
            result["mode"] = "single"
            result["content"] = content

        def on_file_chunk(data, offset, size):
            """Handle file chunk (streaming transfer)."""
            if result["mode"] is None:
                result["mode"] = "chunked"
                result["file_handle"] = open(local_path, "wb")
                result["md5"] = hashlib.md5()

            decoded = base64.b64decode(data)
            result["file_handle"].write(decoded)
            result["md5"].update(decoded)

        def on_file_complete(total_size, checksum):
            """Handle file transfer completion."""
            if result["file_handle"]:
                result["file_handle"].close()
                result["file_handle"] = None
            result["checksum"] = checksum
            result["total_size"] = total_size

        try:
            self.send_request(
                "read_file",
                {"path": remote_path},
                on_file_content=on_file_content,
                on_file_chunk=on_file_chunk,
                on_file_complete=on_file_complete,
                timeout=timeout,
            )

            if result["mode"] == "single" and result["content"] is not None:
                # Small file - decode and write
                data = base64.b64decode(result["content"])
                with open(local_path, "wb") as f:
                    f.write(data)
                return

            elif result["mode"] == "chunked":
                # Large file - already written, verify checksum if available
                if result.get("checksum") and result.get("md5"):
                    local_checksum = result["md5"].hexdigest()
                    if local_checksum != result["checksum"]:
                        raise Exception(
                            f"Checksum mismatch: expected {result['checksum']}, got {local_checksum}"
                        )
                return

            raise Exception(f"Failed to download {remote_path} via agent")

        finally:
            # Ensure file handle is closed on any error
            if result.get("file_handle"):
                result["file_handle"].close()

    def upload_file(self, local_path: str, remote_path: str, timeout: int = None):
        """Uploads a file from local filesystem to the VM.

        Uses chunked uploads for large files to avoid serial buffer overflows.

        Args:
            local_path: Path to local file
            remote_path: Path in VM to write to
            timeout: Optional timeout in seconds (default: scales with file size)
        """
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        if not self.agent_ready:
            raise Exception("Agent not ready")

        with open(local_path, "rb") as f:
            content = f.read()

        file_size = len(content)

        # Calculate timeout based on file size: minimum 60s, +30s per MB
        if timeout is None:
            file_size_mb = file_size / (1024 * 1024)
            timeout = max(60, int(60 + file_size_mb * 30))

        # For files larger than 2KB, use chunked uploads to avoid serial buffer overflows
        # Serial console typically has ~4KB buffer, base64 encoding adds 33% overhead
        # So 2KB raw = ~2.7KB base64 = safe for serial
        CHUNK_SIZE = 2 * 1024  # 2KB chunks

        if file_size <= CHUNK_SIZE:
            # Small file - send in one request
            import base64

            encoded = base64.b64encode(content).decode("utf-8")
            self.send_request(
                "write_file", {"path": remote_path, "content": encoded}, timeout=timeout
            )
        else:
            # Large file - send in chunks with append mode
            import base64

            # First chunk creates the file
            first_chunk = content[:CHUNK_SIZE]
            encoded = base64.b64encode(first_chunk).decode("utf-8")
            self.send_request(
                "write_file",
                {"path": remote_path, "content": encoded, "append": False},
                timeout=timeout,
            )

            # Remaining chunks append
            offset = CHUNK_SIZE
            while offset < file_size:
                chunk = content[offset : offset + CHUNK_SIZE]
                encoded = base64.b64encode(chunk).decode("utf-8")
                self.send_request(
                    "write_file",
                    {"path": remote_path, "content": encoded, "append": True},
                    timeout=timeout,
                )
                offset += CHUNK_SIZE

    def upload_folder(
        self,
        local_path: str,
        remote_path: str,
        pattern: str = None,
        skip_pattern: list[str] = None,
    ):
        """
        Uploads a folder recursively using agent file operations.
        """
        import fnmatch
        from pathlib import Path

        local_path = Path(local_path)
        if not local_path.is_dir():
            raise NotADirectoryError(f"Local path is not a directory: {local_path}")

        if not self.agent_ready:
            raise Exception("Agent not ready")

        for root, dirs, files in os.walk(local_path):
            rel_root = Path(root).relative_to(local_path)
            remote_root = Path(remote_path) / rel_root

            if skip_pattern:
                for d in list(dirs):
                    if any(fnmatch.fnmatch(d, sp) for sp in skip_pattern):
                        dirs.remove(d)

            for d in dirs:
                r_dir = remote_root / d
                logger.debug(f"Creating remote dir: {r_dir}")
                self.send_request(
                    "exec",
                    {
                        "command": f"mkdir -p {r_dir}",
                        "background": False,
                        "env": self.env_vars,
                    },
                )

            for file in files:
                if pattern and not fnmatch.fnmatch(file, pattern):
                    continue
                if skip_pattern and any(
                    fnmatch.fnmatch(file, sp) for sp in skip_pattern
                ):
                    continue

                local_file_path = str(Path(root) / file)
                remote_file_path = str(remote_root / file)

                logger.debug(f"Uploading {local_file_path} to {remote_file_path}")
                self.upload_file(local_file_path, remote_file_path)

    def get_file_info(self, path: str) -> dict:
        """Gets file information (size, mtime, etc.) from the VM."""
        if not self.agent_ready:
            raise Exception("Agent not ready")

        result = {}

        def on_file_info(info):
            result["info"] = info

        self.send_request("file_info", {"path": path}, on_file_info=on_file_info)
        return result.get("info", {})
