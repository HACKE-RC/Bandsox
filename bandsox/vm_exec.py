"""Command/exec and session API for MicroVM (mixin).

Split out of vm.py; recombined into MicroVM via inheritance. These methods
drive the guest agent through transport siblings (send_request, ...) and
networking helpers resolved on `self` via the MRO.
"""
import json
import time
import uuid
import base64
import threading
import logging

logger = logging.getLogger(__name__)


class _ExecMixin:
    def exec_command(self, command: str, on_stdout=None, on_stderr=None, timeout=30):
        """Executes a command in the VM via the agent (blocking).

        When the vsock listener is available, this routes stdout/stderr
        bytes over vsock instead of the serial UART. The agent buffers
        each stream in-VM and uploads them as two separate vsock
        transfers tagged ``<cmd_id>:stdout`` / ``<cmd_id>:stderr`` once
        the command exits, then sends a tiny ``exit`` event back over
        the serial console. The serial console is therefore freed from
        carrying bulk output, which is the bottleneck that made
        concurrent grep/read workloads time out under contention.
        """
        listener = getattr(self, "vsock_listener", None)
        use_vsock = (
            listener is not None
            and getattr(self, "vsock_enabled", False)
            and getattr(listener, "running", False)
        )
        if not use_vsock:
            return self.send_request(
                "exec",
                {"command": command, "background": False, "env": self.env_vars},
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                timeout=timeout,
            )

        port = getattr(self, "vsock_port", None)
        if not port:
            raise RuntimeError(
                "vsock output requested but vsock_port is not set"
            )

        cmd_id = str(uuid.uuid4())
        exec_output_cap = 4 * 1024 * 1024
        stdout_slot = listener.register_pending_buffer(
            cmd_id + ":stdout", max_bytes=exec_output_cap
        )
        stderr_slot = listener.register_pending_buffer(
            cmd_id + ":stderr", max_bytes=exec_output_cap
        )
        logger.debug(
            "vsock-exec cmd_id=%s port=%s listener_path=%s",
            cmd_id,
            port,
            getattr(listener, "listener_path", None),
        )
        exit_metadata = {}
        try:
            rc = self._send_request_with_id(
                cmd_id,
                "exec",
                {
                    "command": command,
                    "background": False,
                    "env": self.env_vars,
                    "use_vsock_output": True,
                    "vsock_port": port,
                },
                # No on_stdout/on_stderr here — the agent uploads via
                # vsock instead of emitting `output` events. If vsock
                # fails inside the agent it falls back to UART output,
                # which we still want to forward to callers:
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                exit_metadata=exit_metadata,
                timeout=timeout,
            )
        except Exception:
            listener.unregister_pending_buffer(cmd_id + ":stdout")
            listener.unregister_pending_buffer(cmd_id + ":stderr")
            raise

        # Newer agents include per-stream vsock metadata in the exit
        # payload. If a stream was confirmed uploaded, give the listener
        # enough time to observe the done marker. Legacy agents and UART
        # fallback keep the short bound because done may never arrive.
        vsock_output = exit_metadata.get("vsock_output")
        if not isinstance(vsock_output, dict):
            vsock_output = {}
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError):
            timeout_seconds = 30.0
        confirmed_upload_wait = min(5.0, max(0.5, timeout_seconds * 0.1))
        legacy_upload_wait = min(2.0, max(0.2, timeout_seconds * 0.02))
        for name, slot in (("stdout", stdout_slot), ("stderr", stderr_slot)):
            uploaded = vsock_output.get(f"{name}_uploaded")
            if uploaded is True:
                wait_timeout = confirmed_upload_wait
            elif uploaded is False:
                wait_timeout = 0.2
            else:
                wait_timeout = legacy_upload_wait
            if not slot["done"].wait(timeout=wait_timeout):
                logger.warning(
                    "vsock-exec %s upload for command %s did not finish within %.1fs; "
                    "bytes may still be pending, continuing with available UART output",
                    name,
                    cmd_id,
                    wait_timeout,
                )
            if slot.get("error"):
                logger.warning(
                    "vsock-exec %s upload for command %s failed: %s",
                    name,
                    cmd_id,
                    slot["error"],
                )
        logger.debug(
            "vsock-exec cmd_id=%s done out_size=%s err_size=%s out_err=%s "
            "err_err=%s out_done=%s err_done=%s",
            cmd_id,
            len(stdout_slot["buf"]),
            len(stderr_slot["buf"]),
            stdout_slot.get("error"),
            stderr_slot.get("error"),
            stdout_slot["done"].is_set(),
            stderr_slot["done"].is_set(),
        )
        try:
            if on_stdout and stdout_slot["done"].is_set() and not stdout_slot.get("error"):
                buf = bytes(stdout_slot["buf"])
                if buf:
                    on_stdout(buf.decode("utf-8", errors="replace"))
            if on_stderr and stderr_slot["done"].is_set() and not stderr_slot.get("error"):
                buf = bytes(stderr_slot["buf"])
                if buf:
                    on_stderr(buf.decode("utf-8", errors="replace"))
        finally:
            listener.unregister_pending_buffer(cmd_id + ":stdout")
            listener.unregister_pending_buffer(cmd_id + ":stderr")
        return rc

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

        with self._event_callbacks_lock:
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
        """Starts a PTY session in the VM.

        If vsock is available, the PTY data plane runs over a dedicated vsock
        connection (raw bytes, no base64). Resize/kill still go over serial.
        Falls back to serial if vsock is unavailable.
        """
        if not self.agent_ready:
            if not self.process and not self.console_conn:
                self.connect_to_console()
            if not self.agent_ready:
                raise Exception("Agent not ready")

        session_id = str(uuid.uuid4())
        use_vsock = self.vsock_enabled and self.vsock_listener is not None

        vsock_slot = None
        if use_vsock:
            vsock_slot = self.vsock_listener.register_pending_pty_session(
                session_id, on_output=on_stdout, on_exit=on_exit
            )

        with self._event_callbacks_lock:
            self.event_callbacks[session_id] = {
                "on_stdout": on_stdout if not use_vsock else None,
                "on_exit": on_exit if not use_vsock else None,
                "_vsock_slot": vsock_slot,
            }

        req = json.dumps(
            {
                "type": "pty_exec",
                "id": session_id,
                "command": command,
                "cols": cols,
                "rows": rows,
                "use_vsock": use_vsock,
            }
        )
        self._write_to_agent(req + "\n")

        return session_id

    def send_session_input(self, session_id: str, data: str, encoding: str = None):
        """Sends input to a session's stdin.

        If the session is using vsock, writes raw bytes directly to the
        vsock connection. Otherwise falls back to serial JSON.
        """
        with self._event_callbacks_lock:
            entry = self.event_callbacks.get(session_id)
            if entry is None:
                return

        vsock_slot = entry.get("_vsock_slot") if entry else None
        if vsock_slot and vsock_slot.conn:
            if encoding == "base64":
                raw = base64.b64decode(data)
            else:
                raw = data.encode("utf-8") if isinstance(data, str) else data
            try:
                vsock_slot.conn.sendall(raw)
                return
            except (OSError, BrokenPipeError):
                pass

        payload = {"type": "input", "id": session_id, "data": data}
        if encoding:
            payload["encoding"] = encoding

        req = json.dumps(payload)
        self._write_to_agent(req + "\n")

    def resize_session(self, session_id: str, cols: int, rows: int):
        """Resizes a PTY session (always via serial — small control message)."""
        with self._event_callbacks_lock:
            if session_id not in self.event_callbacks:
                return

        req = json.dumps(
            {"type": "resize", "id": session_id, "cols": cols, "rows": rows}
        )
        self._write_to_agent(req + "\n")

    def kill_session(self, session_id: str):
        """Kills a session."""
        with self._event_callbacks_lock:
            if session_id not in self.event_callbacks:
                return

        # Clean up vsock PTY session if active
        entry = self.event_callbacks.get(session_id)
        if entry and entry.get("_vsock_slot"):
            if self.vsock_listener:
                self.vsock_listener.unregister_pending_pty_session(session_id)

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
        import requests  # lazy: keeps requests off the VM-boot import path

        return requests.request(method, url, **kwargs)
