import subprocess
import os
import logging
import time
import shutil
import uuid
import threading
import json
import socket
import shlex
import base64
import tempfile
from pathlib import Path
from .firecracker import FirecrackerClient
from .network import setup_tap_device, cleanup_tap_device, derive_host_mac
import requests

logger = logging.getLogger(__name__)

_DIRECT_TEXT_WRITE_MAX_BYTES = 2 * 1024  # ~2 KiB
# Anything bigger goes through _write_bytes which prefers the fastwrite
# RPC (vsock). The previous 512 KiB threshold pushed athena's typical
# 5–50 KiB appends through the serial console — which deadlocked around
# the 4 KiB mark because of Firecracker's tiny UART FIFO. The host saw
# write() succeed, the guest never assembled a full JSON line, and the
# request timed out at 30s.
_SERIAL_WRITE_CHUNK_SIZE = 512 * 1024
_DEBUGFS_FULL_FILE_FALLBACK_LOG_THRESHOLD = 8 * 1024 * 1024


class FastIOError(Exception):
    """Structured error from FastRead/FastWrite RPC.

    Callers (athena UI / agent tools) inspect ``code`` to decide whether
    to retry transient failures (saturated, listener_down, timeout)
    versus surface a real problem to the user (not_found, too_large,
    bad_request, agent_error).
    """

    def __init__(self, code: str, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"[{code}] {msg}")


def _parse_fastio_error(body: bytes) -> Exception:
    """Decode an error frame body into a FastIOError; tolerate old format."""
    try:
        obj = json.loads(body.decode("utf-8"))
        if isinstance(obj, dict) and "code" in obj:
            return FastIOError(obj.get("code", "internal"), obj.get("msg", ""))
    except Exception:
        pass
    return FastIOError("internal", body.decode("utf-8", errors="replace"))


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _child_pids(pid: int) -> list:
    """Return direct child PIDs by scanning /proc."""
    children = []
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return children

    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = entry / "status"
            ppid = None
            with status.open() as f:
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            if ppid == pid:
                children.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
    return children


def _descendant_pids(pid: int) -> list:
    """Return all descendant PIDs, children before grandchildren."""
    descendants = []
    queue = list(_child_pids(pid))
    while queue:
        child = queue.pop(0)
        descendants.append(child)
        queue.extend(_child_pids(child))
    return descendants


def kill_process_tree(pid: int, timeout: float = 1.0):
    """Terminate a process and any descendants, escalating to SIGKILL.

    Firecracker may be started through wrappers such as sudo/ip-netns/nsenter.
    Killing only the wrapper can leave the real firecracker process orphaned,
    so stop paths should tear down the whole tree rooted at the recorded PID.
    """
    import signal

    if not pid or pid == os.getpid():
        return

    targets = list(reversed(_descendant_pids(pid))) + [pid]
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.error(f"Permission denied sending SIGTERM to PID {target}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_pid_exists(target) for target in targets):
            return
        time.sleep(0.05)

    targets = list(reversed(_descendant_pids(pid))) + [pid]
    for target in targets:
        try:
            os.kill(target, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.error(f"Permission denied sending SIGKILL to PID {target}")


FIRECRACKER_BIN = "/usr/bin/firecracker"
DEFAULT_KERNEL_PATH = "/var/lib/bandsox/vmlinux"
DEFAULT_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off"


class ConsoleMultiplexer:
    def __init__(self, socket_path: str, process: subprocess.Popen):
        self.socket_path = socket_path
        self.process = process
        self.clients = []  # list of client sockets
        self.lock = threading.Lock()
        self._input_lock = threading.Lock()
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
        """Writes data to the process stdin.

        Serialized via _input_lock — without it, parallel writers (e.g.
        many fastread handlers) interleave bytes inside the kernel pipe,
        the agent's JSON parser sees garbage and the corresponding reads
        stall until their timeout, fall back to slow serial chunked, and
        produce the 60-second outliers we saw under burst load.
        """
        with self._input_lock:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except Exception as e:
                logger.error(f"Failed to write to process stdin: {e}")

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.server_socket.accept()
                # Big SO_SNDBUF buys headroom so the broadcast loop's
                # sendall doesn't block on slow consumers. With 4 MiB the
                # kernel can absorb a flurry of agent events while athena's
                # python read loop is briefly held under the GIL.
                try:
                    client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                except Exception:
                    pass
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

            # Snapshot callbacks + clients under lock, then release before
            # doing any blocking I/O. Holding the lock during a blocking
            # sendall() (or during a slow callback) stalls this drain thread
            # and, in turn, the firecracker stdout pipe — which eventually
            # blocks the guest agent's stdout.flush() and freezes the VM.
            with self.lock:
                callbacks = list(self.callbacks)
                clients = list(self.clients)

            # Broadcast to callbacks (owner) outside the lock.
            for cb in callbacks:
                try:
                    cb(line)
                except Exception:
                    pass

            # Broadcast to clients outside the lock; per-send timeout bounds
            # how long any single slow client can stall the drain.
            #
            # Bumped 2s → 5s. The 2s budget was too aggressive under
            # athena GIL pressure: a client could fall behind for slightly
            # over 2s and get falsely dropped, surfacing as "Console
            # socket disconnected mid-request" repeatedly even with no
            # actual fault. 5s + 4 MiB SO_SNDBUF on accept (for kernel
            # buffer headroom) handles steady-state GIL hiccups while
            # still bounding how long a truly wedged client can block.
            if clients:
                data = line.encode("utf-8")
                dead_clients = []
                for client in clients:
                    try:
                        client.settimeout(5.0)
                        client.sendall(data)
                    except Exception as exc:
                        dead_clients.append((client, exc))
                    finally:
                        try:
                            client.settimeout(None)
                        except Exception:
                            pass

                if dead_clients:
                    # Log each drop so the next "every command times out"
                    # incident is easy to root-cause from the host log.
                    for client, exc in dead_clients:
                        try:
                            peer = client.getpeername()
                        except Exception:
                            peer = "<unknown>"
                        logger.warning(
                            "Dropping wedged console client %s on %s: %s",
                            peer,
                            self.socket_path,
                            exc,
                        )
                    with self.lock:
                        for client, _ in dead_clients:
                            if client in self.clients:
                                self.clients.remove(client)
                            try:
                                client.close()
                            except Exception:
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
        self.vsock_baked_path = None
        self.vsock_isolation_dir = None
        self._fastread_server = None
        self.fastread_socket_path = None
        self._fastwrite_server = None
        self.fastwrite_socket_path = None
        # New architecture: the host runs a VsockHostListener that accepts
        # guest-initiated AF_VSOCK connections (Firecracker routes them to
        # a Unix socket at <uds_path>_<port>). We no longer keep a long-lived
        # host-side socket connected to Firecracker — the previous design
        # couldn't receive guest-initiated connections at all, which is why
        # every read_file after snapshot restore fell back to the slow
        # serial console.
        self.vsock_listener = None
        # Legacy attributes kept so callers that still poke at them don't
        # crash; not used by the new listener path.
        self.vsock_bridge_socket = None
        self.vsock_bridge_thread = None
        self.vsock_bridge_running = False
        self._agent_write_lock = threading.Lock()

    def start_process(self):
        """Starts the Firecracker process."""
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        cmd = [self.firecracker_bin, "--api-sock", self.socket_path]

        user = os.environ.get("SUDO_USER", os.environ.get("USER", "rc"))

        # If running in NetNS, wrap command
        if self.netns:
            # We must run as root to enter NetNS, but then drop back to user for Firecracker?
            # Firecracker needs to access KVM (usually group kvm).
            # If we run as root inside NetNS, Firecracker creates socket as root.
            # Client (running as user) cannot connect to root socket easily if permissions derived from umask?
            # Better to run: sudo ip netns exec <ns> sudo -u <user> firecracker ...

            # Note: We need full path for sudo if environment is weird, but usually okay.
            if self.vsock_isolation_dir:
                cmd = ["ip", "netns", "exec", self.netns, "sudo", "-u", user] + cmd
            else:
                cmd = [
                    "sudo",
                    "ip",
                    "netns",
                    "exec",
                    self.netns,
                    "sudo",
                    "-u",
                    user,
                ] + cmd
        elif self.vsock_isolation_dir:
            cmd = ["sudo", "-u", user] + cmd

        if self.vsock_isolation_dir:
            cmd = self._wrap_with_vsock_isolation(cmd)

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

    def _wrap_with_vsock_isolation(self, cmd):
        isolation_dir = self.vsock_isolation_dir
        if not isolation_dir:
            return cmd

        tmp_dir = os.path.join(isolation_dir, "tmp")
        vsock_dir = os.path.join(isolation_dir, "vsock")

        mount_cmds = [
            "mount --make-rprivate /",
            f"mkdir -p {shlex.quote(tmp_dir)} {shlex.quote(vsock_dir)} /tmp/bandsox /var/lib/bandsox/vsock",
            f"chmod 0777 {shlex.quote(tmp_dir)} {shlex.quote(vsock_dir)} /tmp/bandsox /var/lib/bandsox/vsock",
            f"mount --bind {shlex.quote(tmp_dir)} /tmp/bandsox",
            f"mount --bind {shlex.quote(vsock_dir)} /var/lib/bandsox/vsock",
        ]

        exec_cmd = shlex.join(cmd)
        shell_cmd = " && ".join(mount_cmds + [f"exec {exec_cmd}"])

        logger.info(f"Starting Firecracker with vsock isolation at {isolation_dir}")
        return ["sudo", "unshare", "-m", "--", "/bin/sh", "-c", shell_cmd]

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
        """Reads from console socket and parses events.

        On disconnect we clear console_conn so the next exec_command
        triggers a fresh connect_to_console() instead of writing to a
        dead socket (which would raise BrokenPipeError every time).

        We accumulate raw bytes and only decode whole lines. The previous
        implementation called bytes.decode('utf-8') on every recv() return,
        which silently raised whenever a multi-byte sequence (or, more
        commonly, kernel log noise containing odd chars) was split across
        the 4096-byte recv boundary. The exception broke the read loop
        and tore down the connection — but more insidiously, when wrapped
        differently it could cause partial drops that surfaced as
        checksum mismatches on chunked file reads.
        """
        buffer = b""
        try:
            while True:
                try:
                    data = self.console_conn.recv(65536)
                    if not data:
                        break
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            decoded = line.decode("utf-8") + "\n"
                        except UnicodeDecodeError:
                            decoded = line.decode("utf-8", errors="replace") + "\n"
                        self._handle_stdout_line(decoded)
                except Exception:
                    break
        finally:
            # Tear down so the next send_request reconnects.
            try:
                self.console_conn.close()
            except Exception:
                pass
            self.console_conn = None
            # Fail any in-flight callbacks so callers don't hang on a
            # completion_event that will never fire — without this, a
            # console drop wedges every concurrent read for the full
            # 30s/60s timeout and athena reports "timed out". The agent
            # may reconnect and resume the next request, but commands
            # already in flight cannot be recovered cleanly.
            try:
                pending = list(self.event_callbacks.items())
                self.event_callbacks.clear()
                for cmd_id, cbs in pending:
                    on_error = cbs.get("on_error")
                    on_exit = cbs.get("on_exit")
                    try:
                        if on_error:
                            on_error("Console socket disconnected mid-request")
                    except Exception:
                        pass
                    try:
                        if on_exit:
                            on_exit(-1)
                    except Exception:
                        pass
            except Exception:
                pass
            logger.warning(
                f"Console socket read loop exited for {self.vm_id}; "
                "will reconnect on next request"
            )

    def _handle_stdout_line(self, line):
        """Parses a line from stdout (event)."""
        import json

        try:
            event = json.loads(line)
            evt_type = event.get("type")
            payload = event.get("payload")

            if evt_type == "status":
                status = payload.get("status")
                cmd_id = payload.get("cmd_id")
                if status == "ready":
                    self.agent_ready = True
                    logger.info("Agent is ready")
                elif status == "started":
                    pid = payload.get("pid")
                    if cmd_id in self.event_callbacks:
                        cb = self.event_callbacks[cmd_id].get("on_started")
                        if cb:
                            cb(pid)

                # Generic status dispatch — used by the vsock fast path
                # to notify the caller that the upload already landed on
                # disk (status == "uploaded") so download_file knows not
                # to wait for chunked serial events.
                if cmd_id and cmd_id in self.event_callbacks:
                    cb = self.event_callbacks[cmd_id].get("on_status")
                    if cb:
                        try:
                            cb(payload)
                        except Exception:
                            pass

            elif evt_type == "output":
                cmd_id = payload.get("cmd_id")
                stream = payload.get("stream")
                data = payload.get("data")
                encoding = payload.get("encoding")
                if encoding == "base64" and isinstance(data, str):
                    try:
                        data = base64.b64decode(data).decode("utf-8", errors="replace")
                    except Exception:
                        pass
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
                    total_lines = payload.get("total_lines")
                    if total_lines is not None:
                        self.event_callbacks[cmd_id]["_agent_total_lines"] = total_lines
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
                    total_lines = payload.get("total_lines")
                    if total_lines is not None:
                        self.event_callbacks[cmd_id]["_agent_total_lines"] = total_lines
                    cb = self.event_callbacks[cmd_id].get("on_file_complete")
                    if cb:
                        cb(total_size, checksum)

            elif evt_type == "exit":
                cmd_id = payload.get("cmd_id")
                exit_code = payload.get("exit_code")
                if cmd_id in self.event_callbacks:
                    payload_cb = self.event_callbacks[cmd_id].get("on_exit_payload")
                    if payload_cb:
                        payload_cb(payload)
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
        on_status=None,
        exit_metadata=None,
        timeout=30,
    ):
        """Sends a JSON request to the agent."""
        cmd_id = str(uuid.uuid4())
        return self._send_request_with_id(
            cmd_id,
            req_type,
            payload,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            on_file_content=on_file_content,
            on_file_chunk=on_file_chunk,
            on_file_complete=on_file_complete,
            on_dir_list=on_dir_list,
            on_file_info=on_file_info,
            on_status=on_status,
            exit_metadata=exit_metadata,
            timeout=timeout,
        )

    def _send_request_with_id(
        self,
        cmd_id: str,
        req_type: str,
        payload: dict,
        on_stdout=None,
        on_stderr=None,
        on_file_content=None,
        on_file_chunk=None,
        on_file_complete=None,
        on_dir_list=None,
        on_file_info=None,
        on_status=None,
        exit_metadata=None,
        timeout=30,
    ):
        """Send a request with a caller-supplied cmd_id.

        Callers that need to pre-register state with the vsock listener
        (e.g. download_file, which tells the listener where to write the
        incoming file) must know the cmd_id before the request is sent,
        so we expose this variant. send_request is the usual entry point
        and just generates a uuid for cmd_id.
        """
        if not self.agent_ready:
            if not self.process and not self.console_conn:
                self.connect_to_console()

            start = time.time()
            while not self.agent_ready:
                if time.time() - start > 10:
                    raise Exception("Agent not ready")
                time.sleep(0.1)

        payload["id"] = cmd_id
        payload["type"] = req_type

        completion_event = threading.Event()
        result = {"code": -1, "error": None}

        def on_exit_payload(payload):
            if exit_metadata is not None:
                exit_metadata.clear()
                exit_metadata.update(payload or {})

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
            "on_status": on_status,
            "on_exit_payload": on_exit_payload,
            "on_exit": on_exit,
            "on_error": on_error,
        }

        req_str = json.dumps(payload)
        self._write_to_agent(req_str + "\n")

        if not completion_event.wait(timeout):
            # Send a kill for this cmd_id so the in-VM child process is
            # actually stopped — otherwise it keeps producing output and
            # monopolises the serial console, wedging every subsequent
            # command on the same VM. Best-effort: a stale callback
            # entry is harmless (it'll be cleaned up when the kill's
            # exit event eventually arrives, or never if the agent is
            # already wedged — in which case the lane is unrecoverable
            # without a VM restart, which is fine because the wedge is
            # what we're preventing in the first place).
            try:
                kill_req = json.dumps({"id": cmd_id, "type": "kill"}) + "\n"
                self._write_to_agent(kill_req)
            except Exception as e:
                logger.debug(
                    "Failed to send kill for timed-out command %s: %s",
                    cmd_id,
                    e,
                    exc_info=True,
                )
            raise TimeoutError("Command timed out")

        # Auto-reconnect-and-retry on transient console drops. Up to 3
        # extra attempts with brief backoff. Hides multiplexer blips and
        # GIL-pressure broadcast drops from athena.
        retry_attempts = 0
        max_retries = 3
        while (
            retry_attempts < max_retries
            and result["error"]
            and "console socket disconnected" in result["error"].lower()
        ):
            retry_attempts += 1
            logger.info(
                f"Console disconnect during cmd {cmd_id}; reconnect+retry "
                f"{retry_attempts}/{max_retries}"
            )
            # Force a fresh connection before retrying.
            try:
                if self.console_conn:
                    self.console_conn.close()
            except Exception:
                pass
            self.console_conn = None
            # Backoff a touch: gives the multiplexer a moment to drain
            # any backlog so we don't immediately get dropped again.
            time.sleep(0.05 * retry_attempts)
            # Reset state for the retry
            result["error"] = None
            result["code"] = -1
            completion_event.clear()
            self.event_callbacks[cmd_id] = {
                "on_stdout": on_stdout,
                "on_stderr": on_stderr,
                "on_file_content": on_file_content,
                "on_file_chunk": on_file_chunk,
                "on_file_complete": on_file_complete,
                "on_dir_list": on_dir_list,
                "on_file_info": on_file_info,
                "on_status": on_status,
                "on_exit_payload": on_exit_payload,
                "on_exit": on_exit,
                "on_error": on_error,
            }
            self._write_to_agent(req_str + "\n")
            if not completion_event.wait(timeout):
                try:
                    kill_req = json.dumps({"id": cmd_id, "type": "kill"}) + "\n"
                    self._write_to_agent(kill_req)
                except Exception as e:
                    logger.debug(
                        "Failed to send kill for timed-out command %s after %s "
                        "reconnect retries: %s",
                        cmd_id,
                        retry_attempts,
                        e,
                        exc_info=True,
                    )
                raise TimeoutError(
                    f"Command timed out (after {retry_attempts} reconnect retries)"
                )

        if result["error"]:
            raise Exception(f"Agent error: {result['error']}")

        return result["code"]

    def _write_to_agent(self, data: str):
        """Writes data to the agent via multiplexer or socket.

        When the console socket is broken (e.g. the runner's multiplexer
        dropped us or the runner restarted), try to reconnect once before
        raising. A single BrokenPipeError without recovery would surface
        as "Error executing command: [Errno 32] Broken pipe" on every
        subsequent tool call, leaving the VM permanently wedged from
        the caller's perspective.
        """
        if self.multiplexer:
            with self._agent_write_lock:
                self.multiplexer.write_input(data)
            return

        payload = data.encode("utf-8")

        with self._agent_write_lock:
            if self.console_conn:
                try:
                    self.console_conn.sendall(payload)
                    return
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    logger.warning(
                        f"Console write failed ({e}); attempting reconnect"
                    )
                    try:
                        self.console_conn.close()
                    except Exception:
                        pass
                    self.console_conn = None

            # No connection or broken — try (re)connect.
            try:
                self.connect_to_console()
            except Exception as e:
                raise Exception(f"No connection to agent: {e}")

            if not self.console_conn:
                raise Exception("No connection to agent")

            self.console_conn.sendall(payload)

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
        disk_bandwidth_mbps: int = 0,
        disk_iops: int = 0,
    ):
        """Configures the VM resources."""
        self.rootfs_path = rootfs_path

        if not boot_args:
            boot_args = f"{DEFAULT_BOOT_ARGS} root=/dev/vda init=/init"

        self.client.put_drives(
            "rootfs",
            rootfs_path,
            is_root_device=True,
            is_read_only=False,
            rate_limit_bandwidth_mbps=disk_bandwidth_mbps,
            rate_limit_iops=disk_iops,
        )

        self.client.put_machine_config(vcpu, mem_mib)
        # Attach virtio-rng for fresh VMs so getrandom() does not block in
        # low-entropy guests (which can stall git/openssl on first use).
        # Older Firecracker builds may not support /entropy; keep startup
        # backwards-compatible in that case.
        try:
            self.client.put_entropy()
        except Exception as e:
            logger.warning(f"Failed to configure entropy device: {e}")

        if enable_networking:
            base_idx = int(self.vm_id[-2:], 16)
            for i in range(50):
                subnet_idx = (base_idx + i) % 253 + 1
                host_ip = f"172.16.{subnet_idx}.1"
                guest_ip = f"172.16.{subnet_idx}.2"
                guest_mac = f"AA:FC:00:00:{subnet_idx:02x}:02"
                host_mac = derive_host_mac(host_ip)

                try:
                    setup_tap_device(self.tap_name, host_ip, host_mac=host_mac)
                    self.network_config = {
                        "host_ip": host_ip,
                        "guest_ip": guest_ip,
                        "guest_mac": guest_mac,
                        "host_mac": host_mac,
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
                    host_mac = derive_host_mac(host_ip)

                    try:
                        setup_tap_device(self.tap_name, host_ip, host_mac=host_mac)
                        self.network_config = {
                            "host_ip": host_ip,
                            "guest_ip": guest_ip,
                            "guest_mac": current_mac,
                            "host_mac": host_mac,
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
            kill_process_tree(self.process.pid, timeout=1)
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            self.process = None

        self._cleanup_vsock_bridge()
        self._cleanup_vsock_isolation()

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
        """Configures Firecracker vsock and starts the host-side listener.

        Firecracker's vsock model:
          - Host-initiated connections: host connect(<uds_path>) + "CONNECT <port>\\n"
          - Guest-initiated connections: host listens on a Unix socket at
            "<uds_path>_<port>", Firecracker forwards guest AF_VSOCK(2, port)
            connections to that listener.

        The previous implementation called ``sock.connect(<uds_path>)`` and
        expected to receive guest-initiated traffic on it, which silently
        never worked — guests got ECONNRESET on every attempt and every
        read_file fell back to the serial console. We now run a proper
        VsockHostListener on ``<uds_path>_<port>`` instead.

        Args:
            cid: Guest Context ID (told to Firecracker via /vsock)
            port: Port the guest agent connects to for file transfers
        """
        from .vsock import VsockHostListener

        self.vsock_socket_path = f"/tmp/bandsox/vsock_{self.vm_id}.sock"
        self.vsock_baked_path = self.vsock_socket_path

        try:
            os.makedirs("/tmp/bandsox", exist_ok=True)
            os.chmod("/tmp/bandsox", 0o777)
        except PermissionError:
            pass

        # Pre-cleanup: Firecracker will create uds_path itself, so only
        # remove stale files. The listener socket (uds_path_port) is
        # ours — VsockHostListener.start() also unlinks it before binding.
        if os.path.exists(self.vsock_socket_path):
            try:
                os.unlink(self.vsock_socket_path)
                logger.debug(f"Removed stale vsock socket: {self.vsock_socket_path}")
            except Exception as e:
                logger.warning(
                    f"Failed to remove stale socket {self.vsock_socket_path}: {e}"
                )

        listener_path = f"{self.vsock_socket_path}_{port}"
        if os.path.exists(listener_path):
            try:
                os.unlink(listener_path)
            except Exception as e:
                logger.warning(
                    f"Failed to remove stale listener socket {listener_path}: {e}"
                )

        try:
            logger.debug(
                f"Configuring Firecracker vsock: CID={cid}, socket={self.vsock_socket_path}"
            )
            self.client.put_vsock("vsock0", cid, self.vsock_socket_path)

            # Firecracker creates <uds_path> asynchronously after the API call
            # returns. We wait up to 5s for it to appear so the listener has
            # a valid parent dir; the listener itself creates its own socket.
            max_wait = 50
            for _ in range(max_wait):
                if os.path.exists(self.vsock_socket_path):
                    break
                time.sleep(0.1)
            else:
                raise Exception(
                    f"Firecracker vsock socket not created at {self.vsock_socket_path}"
                )

            self.vsock_listener = VsockHostListener(
                uds_path=self.vsock_socket_path, port=port
            )
            self.vsock_listener.start()

            self.vsock_enabled = True
            self.vsock_cid = cid
            self.vsock_port = port

            self.env_vars["BANDSOX_VSOCK_PORT"] = str(port)
            logger.info(
                f"Vsock enabled: CID={cid}, port={port}, listener={listener_path}"
            )

            # Bring up the fast-read and fast-write RPC servers so
            # remote ManagedMicroVM callers (athena's webui process)
            # can route file ops through this listener instead of
            # falling back to chunked serial.
            try:
                self._start_fastread_server()
            except Exception as e:
                logger.warning(f"Failed to start fast-read RPC server: {e}")
            try:
                self._start_fastwrite_server()
            except Exception as e:
                logger.warning(f"Failed to start fast-write RPC server: {e}")

        except Exception as e:
            logger.error(f"Failed to setup vsock: {e}")
            self._cleanup_vsock_bridge()
            raise Exception(f"Failed to setup vsock: {e}") from e

    def setup_vsock_listener(self, port: int = None):
        """Start the host-side vsock listener for an already-running VM.

        Used after snapshot restore, where Firecracker has already
        recreated its vsock device from the snapshot and exposed
        ``<uds_path>`` again. We just need to re-attach our listener on
        ``<uds_path>_<port>`` so guest-initiated connections find a
        handler.
        """
        from .vsock import VsockHostListener

        if port is None:
            port = self.vsock_port
        if port is None:
            raise ValueError("No vsock port specified")
        if not self.vsock_socket_path:
            raise ValueError("No vsock socket path configured")

        # Stale listener socket will block bind() with EADDRINUSE; strip it.
        listener_path = f"{self.vsock_socket_path}_{port}"
        if os.path.exists(listener_path):
            try:
                os.unlink(listener_path)
            except Exception as e:
                logger.debug(f"Failed to remove stale listener {listener_path}: {e}")

        self.vsock_listener = VsockHostListener(
            uds_path=self.vsock_socket_path, port=port
        )
        self.vsock_listener.start()

        self.vsock_enabled = True
        self.vsock_port = port
        self.env_vars["BANDSOX_VSOCK_PORT"] = str(port)
        logger.info(
            f"Vsock listener started for restored VM: port={port}, path={listener_path}"
        )

        # Bring up the fast-read AND fast-write RPC servers alongside
        # the listener so any remote ManagedMicroVM (e.g. athena's webui
        # process, which lives outside the runner's mount namespace) can
        # route file ops through this listener instead of falling back
        # to the slow serial path.
        try:
            self._start_fastread_server()
        except Exception as e:
            logger.warning(f"Failed to start fast-read RPC server: {e}")
        try:
            self._start_fastwrite_server()
        except Exception as e:
            logger.warning(f"Failed to start fast-write RPC server: {e}")

    def _fastread_socket_path_for_remote(self) -> str:
        """Return the canonical fastread UDS path the runner would expose.

        Used by remote callers (ManagedMicroVM) that don't own a local
        listener but want to ask the runner to do a vsock read on their
        behalf.
        """
        if getattr(self, "fastread_socket_path", None):
            return self.fastread_socket_path
        try:
            from .vsock.fastread_server import fastread_socket_path_for
            return fastread_socket_path_for(self.socket_path, self.vm_id)
        except Exception:
            return None

    def _fastread_remote(self, sock_path: str, path: str, timeout: float = 60.0, op: str = "read") -> bytes:
        """Synchronous fast-read RPC client.

        Connects to the runner's fastread UDS, sends the request, returns
        the response bytes (file content for op="read", JSON-encoded
        listing for op="list_dir"). On error raises FastIOError with
        .code and .msg.
        """
        import struct as _struct
        import uuid as _uuid

        cmd_id = str(_uuid.uuid4())
        body = json.dumps({"path": path, "cmd_id": cmd_id, "op": op}).encode("utf-8")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(sock_path)
            sock.sendall(_struct.pack(">I", len(body)) + body)

            hdr = self._recvn(sock, 4)
            (length,) = _struct.unpack(">I", hdr)
            if length == 0xFFFFFFFF:
                err_hdr = self._recvn(sock, 4)
                (err_len,) = _struct.unpack(">I", err_hdr)
                err_body = self._recvn(sock, err_len) if err_len else b""
                raise _parse_fastio_error(err_body)
            if length == 0:
                return b""
            return self._recvn(sock, length)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _fastwrite_socket_path_for_remote(self) -> str:
        if getattr(self, "fastwrite_socket_path", None):
            return self.fastwrite_socket_path
        try:
            from .vsock.fastwrite_server import fastwrite_socket_path_for
            return fastwrite_socket_path_for(self.socket_path, self.vm_id)
        except Exception:
            return None

    def _fastwrite_remote(
        self,
        sock_path: str,
        remote_path: str,
        content: bytes,
        append: bool = False,
        timeout: float = 60.0,
    ):
        """Synchronous fast-write RPC client. Raises on error."""
        import struct as _struct
        import uuid as _uuid

        cmd_id = str(_uuid.uuid4())
        header = json.dumps(
            {"op": "write", "path": remote_path, "cmd_id": cmd_id, "append": bool(append)}
        ).encode("utf-8")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(sock_path)
            sock.sendall(_struct.pack(">I", len(header)) + header)
            sock.sendall(_struct.pack(">I", len(content)))
            if content:
                sock.sendall(content)

            hdr = self._recvn(sock, 4)
            (length,) = _struct.unpack(">I", hdr)
            if length == 0xFFFFFFFF:
                err_hdr = self._recvn(sock, 4)
                (err_len,) = _struct.unpack(">I", err_hdr)
                err_body = self._recvn(sock, err_len) if err_len else b""
                raise _parse_fastio_error(err_body)
            # length == 0 → success, no payload.
        finally:
            try:
                sock.close()
            except Exception:
                pass

    @staticmethod
    def _recvn(sock: socket.socket, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = sock.recv(min(65536, n - len(out)))
            if not chunk:
                raise ConnectionResetError("server closed mid-frame")
            out.extend(chunk)
        return bytes(out)

    def _start_fastread_server(self):
        """Start the fast-read RPC server for this VM.

        Idempotent — re-binds if already running. Safe to call from the
        runner (where vsock_listener is local) but a no-op on processes
        without a local listener.
        """
        from .vsock.fastread_server import (
            FastReadServer,
            fastread_socket_path_for,
        )

        if self.vsock_listener is None:
            return
        if getattr(self, "_fastread_server", None) is not None:
            return

        sock_path = fastread_socket_path_for(self.socket_path, self.vm_id)

        def _writer(data: str):
            self._write_to_agent(data)

        # Pass a getter rather than the listener instance so the server
        # tracks supervisor-driven listener restarts.
        srv = FastReadServer(
            socket_path=sock_path,
            vsock_listener=lambda: self.vsock_listener,
            write_to_agent=_writer,
            vsock_port=self.vsock_port,
        )
        srv.start()
        self._fastread_server = srv
        self.fastread_socket_path = sock_path

    def _start_fastwrite_server(self):
        from .vsock.fastwrite_server import (
            FastWriteServer,
            fastwrite_socket_path_for,
        )

        if self.vsock_listener is None:
            return
        if getattr(self, "_fastwrite_server", None) is not None:
            return

        sock_path = fastwrite_socket_path_for(self.socket_path, self.vm_id)
        srv = FastWriteServer(
            socket_path=sock_path,
            vsock_listener=lambda: self.vsock_listener,
            send_request_with_id=self._send_request_with_id,
            vsock_port=self.vsock_port,
        )
        srv.start()
        self._fastwrite_server = srv
        self.fastwrite_socket_path = sock_path

    def _cleanup_vsock_bridge(self):
        """Stop the vsock listener and release its socket.

        We do NOT delete ``vsock_socket_path`` — Firecracker owns that file
        and will unlink it when the VM exits. We only unlink the listener
        socket at ``<uds_path>_<port>`` inside VsockHostListener.stop().
        """
        logger.debug(f"Cleaning up vsock for {self.vm_id}")

        if self.vsock_listener is not None:
            try:
                self.vsock_listener.stop()
            except Exception as e:
                logger.debug(f"Error stopping vsock listener: {e}")
            self.vsock_listener = None

        # Legacy bridge cleanup — kept for any caller still writing to
        # these attributes from older code paths.
        self.vsock_bridge_running = False
        if self.vsock_bridge_socket is not None:
            try:
                self.vsock_bridge_socket.close()
            except Exception:
                pass
            self.vsock_bridge_socket = None
        if self.vsock_bridge_thread is not None and self.vsock_bridge_thread.is_alive():
            try:
                self.vsock_bridge_thread.join(timeout=1)
            except Exception:
                pass
            self.vsock_bridge_thread = None

        self.vsock_socket_path = None
        self.vsock_baked_path = None
        self.vsock_enabled = False
        self.vsock_cid = None
        self.vsock_port = None

        if "BANDSOX_VSOCK_PORT" in self.env_vars:
            del self.env_vars["BANDSOX_VSOCK_PORT"]

    def _cleanup_vsock_isolation(self):
        if not self.vsock_isolation_dir:
            return
        try:
            shutil.rmtree(self.vsock_isolation_dir)
            logger.debug(f"Removed vsock isolation dir: {self.vsock_isolation_dir}")
        except Exception as e:
            logger.warning(
                f"Failed to remove vsock isolation dir {self.vsock_isolation_dir}: {e}"
            )
        self.vsock_isolation_dir = None

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

    def _has_debugfs_rootfs(self) -> bool:
        rootfs_path = getattr(self, "rootfs_path", None)
        return bool(rootfs_path and os.path.exists(rootfs_path))

    def _debugfs_download_file(self, remote_path: str, local_path: str) -> None:
        """Read a file directly from the ext4 rootfs with debugfs.

        This path is used when the guest agent is unavailable but the rootfs
        image is still accessible on the host. We pause/resume the VM
        best-effort when we own a live Firecracker socket to reduce the risk
        of reading a mutating filesystem.
        """
        if not self._has_debugfs_rootfs():
            raise Exception("debugfs fallback unavailable: rootfs_path is missing")

        rootfs_path = os.path.abspath(self.rootfs_path)
        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        remote_quoted = remote_path.replace('"', '\\"')
        paused = False
        try:
            if getattr(self, "socket_path", None) and os.path.exists(self.socket_path):
                try:
                    self.pause()
                    paused = True
                except Exception as exc:
                    logger.warning(f"Failed to pause VM before debugfs read: {exc}")

            cmd = [
                "debugfs",
                "-R",
                f"dump -p \"{remote_quoted}\" \"{local_path}\"",
                rootfs_path,
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
            )
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            combined = f"{stderr}\n{stdout}".lower()
            if proc.returncode != 0 or any(
                marker in combined
                for marker in (
                    "file not found",
                    "not found by ext2_lookup",
                    "no such file",
                    "does not exist",
                )
            ):
                detail = stderr or stdout or f"debugfs exited with {proc.returncode}"
                raise Exception(detail)
        finally:
            if paused:
                try:
                    self.resume()
                except Exception as exc:
                    logger.warning(f"Failed to resume VM after debugfs read: {exc}")

    def get_file_contents(
        self,
        path: str,
        offset: int = 0,
        limit: int = 0,
        show_line_numbers: bool = False,
        show_header: bool = True,
        show_footer: bool = True,
    ) -> str:
        """Reads the contents of a file inside the VM.

        Args:
            path: File path in VM
            offset: Lines to skip from beginning (0 = start at line 1)
            limit: Max lines to return (0 = unlimited)
            show_line_numbers: Prefix each line with "N\\t"
            show_header: If offset>0, show "... skipped N lines" header
            show_footer: If limit>0 and more lines remain, show "... N lines left" footer

        The agent reports total line count alongside the content, so
        offset/limit slicing happens server-side even when header/footer is
        requested — the full file is never transferred over vsock/serial
        just for decoration.
        """
        agent_error = None
        if self.agent_ready:
            import base64
            import hashlib

            raw_bytes = None
            raw_bytes_full_file = False

            # Fast-read RPC: when the local process doesn't own the vsock
            # listener (e.g. athena's webui talking to a detached runner),
            # ask the runner to do the read via its in-namespace listener
            # and stream the bytes back over a side-channel UDS. This is
            # what unlocks vsock speed for ManagedMicroVM callers.
            if self.vsock_listener is None and offset == 0 and limit == 0 and not show_line_numbers:
                fr_path = self._fastread_socket_path_for_remote()
                if fr_path:
                    # Briefly poll for the socket — closes the race where
                    # the runner is still binding its fastread server when
                    # athena issues the first read after restore_vm. Without
                    # this, the first athena read loses to the listener-bind
                    # and silently falls back to the slow serial path.
                    deadline = time.time() + 1.5
                    while not os.path.exists(fr_path) and time.time() < deadline:
                        time.sleep(0.025)
                    if os.path.exists(fr_path):
                        # Retry once on transient errors (saturated, listener_down,
                        # connection reset). One retry hides multiplexer/runner
                        # blips from athena's UI without masking real failures
                        # like not_found.
                        for attempt in range(2):
                            try:
                                raw_bytes = self._fastread_remote(fr_path, path)
                                raw_bytes_full_file = True
                                # Empty buffer with no error usually means
                                # the listener slot fired before the agent
                                # actually uploaded — a race. Treat as a
                                # soft failure on first attempt so we
                                # retry; on second, fall through to serial
                                # so the caller doesn't get a phantom-empty.
                                if not raw_bytes and attempt == 0:
                                    logger.info(
                                        f"FastRead returned 0 bytes for {path}; retrying"
                                    )
                                    raw_bytes = None
                                    time.sleep(0.05)
                                    continue
                                if not raw_bytes:
                                    logger.info(
                                        f"FastRead still 0 bytes for {path}; falling through to serial"
                                    )
                                    raw_bytes = None
                                break
                            except FastIOError as exc:
                                if attempt == 0 and exc.code in (
                                    "saturated", "listener_down", "timeout", "internal"
                                ):
                                    logger.info(
                                        f"FastRead transient {exc.code} for {path}; retrying"
                                    )
                                    time.sleep(0.05)
                                    continue
                                logger.info(
                                    f"FastRead RPC failed for {path}: {exc}"
                                )
                                raw_bytes = None
                                break
                            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                                if attempt == 0:
                                    logger.info(
                                        f"FastRead transient socket error for {path}: {exc}; retrying"
                                    )
                                    time.sleep(0.05)
                                    continue
                                logger.info(
                                    f"FastRead RPC failed for {path}: {exc}"
                                )
                                raw_bytes = None
                                break
                            except Exception as exc:
                                logger.info(
                                    f"FastRead RPC failed for {path} via {fr_path}: {exc}"
                                )
                                raw_bytes = None
                                break

            # Vsock fast path: ask the guest to upload the file directly into
            # an in-memory buffer on the listener. We do NOT synchronously
            # wait for the agent's serial "exit" event — that would queue
            # N exits through the single serial console under heavy parallel
            # reads. Instead we wait on the listener's done-event, which
            # fires the moment the bytes have all landed. The exit/status
            # serial events arrive asynchronously and clean up callbacks
            # via the existing dispatch loop.
            vsock_ready = (
                raw_bytes is None
                and self.vsock_enabled
                and self.vsock_listener is not None
                and getattr(self, "vsock_port", None)
                and offset == 0
                and limit == 0
                and not show_line_numbers
            )
            if vsock_ready:
                cmd_id = str(uuid.uuid4())
                slot = self.vsock_listener.register_pending_buffer(cmd_id)

                # Lightweight callback set so _handle_stdout_line can clean
                # up event_callbacks when the agent's exit event eventually
                # arrives. We don't block on it.
                vsock_error_holder = {"err": None, "exited_nonzero": False}

                def _vsock_on_error(msg, _e=vsock_error_holder, _slot=slot):
                    _e["err"] = msg
                    # Wake the waiter — guest reported failure (e.g. file not
                    # found) and will never upload, so don't block 60s.
                    _slot["done"].set()

                def _vsock_on_exit(code, _e=vsock_error_holder, _slot=slot):
                    if code != 0:
                        _e["exited_nonzero"] = True
                        _slot["done"].set()

                self.event_callbacks[cmd_id] = {
                    "on_stdout": None,
                    "on_stderr": None,
                    "on_file_content": None,
                    "on_file_chunk": None,
                    "on_file_complete": None,
                    "on_dir_list": None,
                    "on_file_info": None,
                    "on_status": None,
                    "on_exit": _vsock_on_exit,
                    "on_error": _vsock_on_error,
                }

                payload_dict = {
                    "id": cmd_id,
                    "type": "read_file",
                    "path": path,
                    "use_vsock": True,
                    "vsock_port": self.vsock_port,
                }
                try:
                    self._write_to_agent(json.dumps(payload_dict) + "\n")
                    woke = slot["done"].wait(timeout=60)
                    if vsock_error_holder["err"]:
                        # Guest reported an error (e.g. file not found).
                        # Don't fall back to serial — it'd hit the same error.
                        raise Exception(vsock_error_holder["err"])
                    if not woke:
                        raise TimeoutError(f"Vsock read of {path} timed out")
                    if slot["error"]:
                        raise Exception(slot["error"])
                    if vsock_error_holder["exited_nonzero"] and not slot["buf"]:
                        raise Exception("Agent exited non-zero before upload")
                    raw_bytes = bytes(slot["buf"])
                    raw_bytes_full_file = True
                except Exception as exc:
                    # Use INFO so we can see in the bench/server log when the
                    # fast path is being skipped or failing — without this, a
                    # silent fallback to serial looks identical to "vsock
                    # never tried".
                    logger.info(f"Vsock read fast-path failed for {path}: {exc}")
                    raw_bytes = None
                    raw_bytes_full_file = False
                finally:
                    self.vsock_listener.unregister_pending_buffer(cmd_id)

            if raw_bytes is None:
                result = {
                    "mode": None,
                    "content": None,
                    "chunks": bytearray(),
                    "checksum": None,
                    "total_size": None,
                }

                def on_file_content(c):
                    result["mode"] = "single"
                    result["content"] = c

                def on_file_chunk(data, offset, size):
                    if result["mode"] is None:
                        result["mode"] = "chunked"
                    result["chunks"].extend(base64.b64decode(data))

                def on_file_complete(total_size, checksum):
                    result["total_size"] = total_size
                    result["checksum"] = checksum

                # Retry the agent read up to 3 times on empty/missing content
                # before giving up. The agent under heavy concurrency can
                # emit a file_content event with empty content (or drop the
                # event entirely after the retry-storm rebuilt the connection)
                # — that surfaces as a phantom empty file in athena's UI.
                # We detect "empty result with no error" and try again with a
                # fresh result dict.
                last_attempt_was_empty = False
                for serial_attempt in range(3):
                    # Fresh result dict per attempt so a stale empty from a
                    # prior attempt doesn't poison this one.
                    result = {
                        "mode": None,
                        "content": None,
                        "chunks": bytearray(),
                        "checksum": None,
                        "total_size": None,
                        "agent_total_lines": None,
                    }
                    cmd_id = str(uuid.uuid4())
                    def on_file_content(c, _r=result):
                        _r["mode"] = "single"
                        _r["content"] = c
                        _ec = self.event_callbacks.get(cmd_id, {})
                        _tls = _ec.get("_agent_total_lines")
                        if _tls is not None:
                            _r["agent_total_lines"] = _tls
                    def on_file_chunk(data, offset_, size, _r=result):
                        if _r["mode"] is None:
                            _r["mode"] = "chunked"
                        _r["chunks"].extend(base64.b64decode(data))
                    def on_file_complete(total_size, checksum, _r=result):
                        _r["total_size"] = total_size
                        _r["checksum"] = checksum
                        _ec = self.event_callbacks.get(cmd_id, {})
                        _tls = _ec.get("_agent_total_lines")
                        if _tls is not None:
                            _r["agent_total_lines"] = _tls

                    try:
                        self._send_request_with_id(
                            cmd_id,
                            "read_file",
                            {
                                "path": path,
                                "offset": offset,
                                "limit": limit,
                                "show_line_numbers": show_line_numbers,
                            },
                            on_file_content=on_file_content,
                            on_file_chunk=on_file_chunk,
                            on_file_complete=on_file_complete,
                        )
                        if result["mode"] == "single" and result["content"] is not None:
                            raw_bytes = base64.b64decode(result["content"])
                        elif result["mode"] == "chunked":
                            if result["checksum"]:
                                md5 = hashlib.md5(result["chunks"]).hexdigest()
                                if md5 != result["checksum"]:
                                    raise Exception(
                                        f"Checksum mismatch: expected {result['checksum']}, got {md5}"
                                    )
                            raw_bytes = bytes(result["chunks"])
                        # Phantom-empty detection: agent acked the read but
                        # the content callback either fired with "" or never
                        # fired at all. Retry — the agent's view of the file
                        # is fine, our pipeline raced.
                        if raw_bytes is None or (raw_bytes == b"" and (limit > 0 or offset > 0)):
                            if serial_attempt < 2:
                                logger.info(
                                    f"Agent read of {path} returned empty (mode={result['mode']}, "
                                    f"attempt {serial_attempt + 1}/3); retrying"
                                )
                                last_attempt_was_empty = True
                                raw_bytes = None
                                time.sleep(0.05 * (serial_attempt + 1))
                                continue
                            # Out of retries — surface as failure so the
                            # caller (athena) doesn't render a phantom-empty
                            # markdown block. Better a visible error than
                            # silent corruption.
                            raise Exception(
                                f"Agent read of {path} returned empty after 3 attempts"
                            )
                        break
                    except Exception as exc:
                        agent_error = exc
                        logger.warning(
                            f"Agent read failed for {path}; trying debugfs fallback: {exc}"
                        )
                        break

            if raw_bytes is not None:
                agent_tl = result.get("agent_total_lines")
                if show_header or show_footer or show_line_numbers:
                    return self._format_file_content(
                        raw_bytes, offset, limit, show_line_numbers,
                        show_header, show_footer, total_lines=agent_tl,
                    )
                return raw_bytes.decode("utf-8", errors="replace")

        if self._has_debugfs_rootfs():
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                temp_path = tmp.name
            try:
                try:
                    self._debugfs_download_file(path, temp_path)
                except Exception as dfs_err:
                    if agent_error is not None:
                        raise agent_error
                    raise dfs_err
                with open(temp_path, "rb") as f:
                    raw_bytes = f.read()
                if (
                    len(raw_bytes) >= _DEBUGFS_FULL_FILE_FALLBACK_LOG_THRESHOLD
                    and (offset > 0 or limit > 0 or show_line_numbers)
                ):
                    logger.warning(
                        "debugfs fallback read %s bytes from %s for host-side "
                        "file formatting",
                        len(raw_bytes),
                        path,
                    )
                return self._format_file_content(
                    raw_bytes, offset, limit, show_line_numbers, show_header, show_footer
                )
            finally:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

        if agent_error is not None:
            raise agent_error
        raise Exception(
            f"Failed to read {path}: agent unavailable and debugfs fallback unavailable"
        )

    @staticmethod
    def _format_file_content(
        raw_bytes: bytes,
        offset: int = 0,
        limit: int = 0,
        show_line_numbers: bool = False,
        show_header: bool = True,
        show_footer: bool = True,
        total_lines: int = None,
    ) -> str:
        """Apply offset, limit, line numbers, header, and footer to raw file content.

        When *total_lines* is provided, the content is assumed to be pre-sliced
        by the agent (offset/limit already applied) and total_lines is used for
        the header/footer decoration instead of computing it locally.  This
        avoids pulling the entire file over vsock/serial just for the line
        count.
        """
        text = raw_bytes.decode("utf-8", errors="replace")
        lines = text.split("\n")

        if total_lines is not None:
            # Agent already sliced; lines is just the requested window.
            selected = lines
            _total = total_lines
            _start = offset
        else:
            # Legacy path: full file content, slice locally.
            _total = len(lines)
            _start = min(offset, _total)
            _end = _total if limit == 0 else min(_start + limit, _total)
            selected = lines[_start:_end]

        _end = _start + len(selected)
        result_lines = []

        # Header: skipped lines indicator
        if show_header and _start > 0:
            result_lines.append(f"... skipped {_start} lines")

        # Line numbers
        if show_line_numbers:
            for i, line in enumerate(selected, start=_start + 1):
                result_lines.append(f"{i}\t{line}")
        else:
            result_lines.extend(selected)

        # Footer: remaining lines indicator
        if show_footer and _end < _total:
            remaining = _total - _end
            result_lines.append(f"... {remaining} lines left")

        return "\n".join(result_lines)

    def list_dir(self, path: str) -> list:
        """Lists directory contents.

        Uses the fast-read RPC when available (athena → runner) to bypass
        the slow serial console for the JSON listing. Falls back to the
        legacy serial path if the fastread socket isn't reachable.
        """
        if not self.agent_ready:
            raise Exception("Agent not ready")

        # FastRead RPC list_dir variant — same socket, op="list_dir"
        if self.vsock_listener is None:
            fr_path = self._fastread_socket_path_for_remote()
            if fr_path:
                deadline = time.time() + 1.5
                while not os.path.exists(fr_path) and time.time() < deadline:
                    time.sleep(0.025)
                if os.path.exists(fr_path):
                    try:
                        raw = self._fastread_remote(fr_path, path, op="list_dir")
                        if raw:
                            payload = json.loads(raw.decode("utf-8"))
                            return payload.get("files", [])
                    except Exception as exc:
                        logger.info(f"FastRead list_dir failed for {path}: {exc}")

        # Local-listener vsock fast path: send list_dir use_vsock=True and
        # wait on a registered buffer.
        if (
            self.vsock_enabled
            and self.vsock_listener is not None
            and getattr(self, "vsock_port", None)
        ):
            try:
                cmd_id = str(uuid.uuid4())
                slot = self.vsock_listener.register_pending_buffer(cmd_id)
                self.event_callbacks[cmd_id] = {
                    "on_stdout": None, "on_stderr": None,
                    "on_file_content": None, "on_file_chunk": None,
                    "on_file_complete": None, "on_dir_list": None,
                    "on_file_info": None, "on_status": None,
                    "on_exit": lambda code, _s=slot: code != 0 and _s["done"].set(),
                    "on_error": lambda msg, _s=slot: _s["done"].set(),
                }
                self._write_to_agent(json.dumps({
                    "id": cmd_id, "type": "list_dir",
                    "path": path, "use_vsock": True, "vsock_port": self.vsock_port,
                }) + "\n")
                if slot["done"].wait(timeout=15) and slot["buf"] and not slot["error"]:
                    payload = json.loads(bytes(slot["buf"]).decode("utf-8"))
                    return payload.get("files", [])
            except Exception as exc:
                logger.info(f"Local vsock list_dir failed for {path}: {exc}")
            finally:
                try:
                    self.vsock_listener.unregister_pending_buffer(cmd_id)
                except Exception:
                    pass

        result = {}

        def on_dir_list(files):
            result["files"] = files

        self.send_request("list_dir", {"path": path}, on_dir_list=on_dir_list)
        return result.get("files", [])

    def download_file(self, remote_path: str, local_path: str, timeout: int = 300):
        """Downloads a file from the VM to the local filesystem.

        Prefers the vsock fast path when the listener is running: we register
        a pending upload under a pre-allocated cmd_id, send ``read_file`` with
        that id, and the guest uploads the file directly to the listener which
        writes it to ``local_path`` at native speed. If vsock is unavailable
        the agent automatically falls back to chunked serial (file_chunk
        events) which we handle below.

        Args:
            remote_path: Path to file in VM
            local_path: Path to save file locally
            timeout: Timeout in seconds (default 300 for large files over serial)
        """
        if not self.agent_ready:
            if self._has_debugfs_rootfs():
                self._debugfs_download_file(remote_path, local_path)
                return
            raise Exception("Agent not ready")

        import base64
        import hashlib

        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        cmd_id = str(uuid.uuid4())

        # Pre-register the upload path with the listener so the guest's vsock
        # connection can be routed to the right destination file even though
        # the request itself travels over serial.
        if self.vsock_enabled and self.vsock_listener is not None:
            self.vsock_listener.register_pending_upload(cmd_id, local_path)

        result = {
            "mode": None,
            "content": None,
            "file_handle": None,
            "md5": None,
            "error": None,
            "vsock_success": False,
        }

        def on_file_content(content):
            """Handle small file (single shot transfer over serial)."""
            result["mode"] = "single"
            result["content"] = content

        def on_file_chunk(data, offset, size):
            """Handle file chunk (streaming transfer over serial)."""
            if result["mode"] is None:
                result["mode"] = "chunked"
                result["file_handle"] = open(local_path, "wb")
                result["md5"] = hashlib.md5()

            decoded = base64.b64decode(data)
            result["file_handle"].write(decoded)
            result["md5"].update(decoded)

        def on_file_complete(total_size, checksum):
            """Handle file transfer completion (serial chunked path)."""
            if result["file_handle"]:
                result["file_handle"].close()
                result["file_handle"] = None
            result["checksum"] = checksum
            result["total_size"] = total_size

        def on_status(payload):
            """Pick up the vsock fast-path completion signal."""
            if payload.get("status") == "uploaded":
                result["vsock_success"] = True
                result["mode"] = "vsock"

        agent_error = None
        try:
            payload = {"path": remote_path, "use_vsock": bool(self.vsock_enabled)}
            if self.vsock_port:
                payload["vsock_port"] = self.vsock_port
            self._send_request_with_id(
                cmd_id,
                "read_file",
                payload,
                on_file_content=on_file_content,
                on_file_chunk=on_file_chunk,
                on_file_complete=on_file_complete,
                on_status=on_status,
                timeout=timeout,
            )

            if result["mode"] == "vsock" and result["vsock_success"]:
                if not os.path.exists(local_path):
                    raise Exception(
                        f"Vsock transfer reported success but file not found: {local_path}"
                    )
                return

            if result["mode"] == "single" and result["content"] is not None:
                data = base64.b64decode(result["content"])
                with open(local_path, "wb") as f:
                    f.write(data)
                return

            if result["mode"] == "chunked":
                if result.get("checksum") and result.get("md5"):
                    local_checksum = result["md5"].hexdigest()
                    if local_checksum != result["checksum"]:
                        raise Exception(
                            f"Checksum mismatch: expected {result['checksum']}, got {local_checksum}"
                        )
                return

            raise Exception(f"Failed to download {remote_path} via agent")
        except Exception as exc:
            agent_error = exc

        finally:
            if result.get("file_handle"):
                result["file_handle"].close()
            # Drop any registration we made so the listener doesn't hold
            # a reference to local_path beyond this call.
            if self.vsock_enabled and self.vsock_listener is not None:
                self.vsock_listener.unregister_pending_upload(cmd_id)

        if self._has_debugfs_rootfs():
            logger.warning(
                f"Agent download path failed for {remote_path}; trying debugfs fallback: {agent_error}"
            )
            self._debugfs_download_file(remote_path, local_path)
            return

        raise Exception(
            f"Failed to download {remote_path} via agent and debugfs fallback unavailable: {agent_error}"
        )

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        timeout: int = None,
        append: bool = False,
    ):
        """Uploads a file from local filesystem to the VM.

        Uses the vsock fast path when available (guest downloads directly from
        listener), falling back to chunked serial uploads.

        Args:
            local_path: Path to local file
            remote_path: Path in VM to write to
            timeout: Optional timeout in seconds (default: scales with file size)
            append: If True, append to remote_path instead of overwriting it.
        """
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        if not self.agent_ready:
            raise Exception("Agent not ready")

        with open(local_path, "rb") as f:
            content = f.read()

        self._write_bytes(remote_path, content, timeout=timeout, append=append)

    def write_text(
        self,
        remote_path: str,
        content: str,
        timeout: int = None,
        append: bool = False,
    ):
        """Write UTF-8 text directly to a file in the VM.

        Small and medium text writes go as one JSON request without creating a
        host temp file. Larger payloads use the raw bytes path so vsock can take
        over when available, with serial chunking as the compatibility fallback.
        """
        if not self.agent_ready:
            raise Exception("Agent not ready")

        encoded = (content or "").encode("utf-8")
        if len(encoded) <= _DIRECT_TEXT_WRITE_MAX_BYTES:
            if timeout is None:
                timeout = 30
            self.send_request(
                "write_text",
                {"path": remote_path, "content": content or "", "append": append},
                timeout=timeout,
            )
            return

        self._write_bytes(remote_path, encoded, timeout=timeout, append=append)

    def append_text(self, remote_path: str, content: str, timeout: int = None):
        """Append UTF-8 text directly to a file in the VM."""
        self.write_text(remote_path, content, timeout=timeout, append=True)

    def write_bytes(
        self,
        remote_path: str,
        content: bytes,
        timeout: int = None,
        append: bool = False,
    ):
        """Write bytes to a file in the VM without requiring a local temp file."""
        self._write_bytes(remote_path, bytes(content), timeout=timeout, append=append)

    def _write_bytes(
        self,
        remote_path: str,
        content: bytes,
        timeout: int = None,
        append: bool = False,
    ):
        if not self.agent_ready:
            raise Exception("Agent not ready")

        file_size = len(content)

        # Calculate timeout based on file size: minimum 30s, +10s per MB
        if timeout is None:
            file_size_mb = file_size / (1024 * 1024)
            timeout = max(30, int(30 + file_size_mb * 10))

        # FastWrite RPC: when we don't own a local vsock listener (e.g.
        # athena's webui talking to a detached runner) ask the runner to
        # do the write via its in-namespace listener. This unlocks vsock
        # speed for ManagedMicroVM uploads — without it large writes go
        # through the serial chunked path and contend with reads.
        if (
            getattr(self, "vsock_listener", None) is None
            and file_size > 512
        ):
            fw_path = self._fastwrite_socket_path_for_remote()
            if fw_path:
                deadline = time.time() + 1.5
                while not os.path.exists(fw_path) and time.time() < deadline:
                    time.sleep(0.025)
                if os.path.exists(fw_path):
                    try:
                        self._fastwrite_remote(
                            fw_path, remote_path, content, append=append, timeout=timeout
                        )
                        return
                    except Exception as exc:
                        logger.info(
                            f"FastWrite RPC failed for {remote_path}: {exc}"
                        )

        # Vsock fast path: register content for download, tell guest to fetch it.
        if (
            getattr(self, "vsock_enabled", False)
            and getattr(self, "vsock_listener", None) is not None
            and file_size > 512  # Small files are faster via serial
        ):
            cmd_id = str(uuid.uuid4())
            self.vsock_listener.register_pending_download(cmd_id, content)
            try:
                self._send_request_with_id(
                    cmd_id,
                    "write_file_vsock",
                    {
                        "path": remote_path,
                        "vsock_port": self.vsock_port,
                        "append": append,
                    },
                    timeout=timeout,
                )
                return
            except Exception:
                logger.debug(
                    f"Vsock write_file failed, falling back to serial for {remote_path}"
                )
            finally:
                self.vsock_listener.unregister_pending_download(cmd_id)

        import base64

        CHUNK_SIZE = _SERIAL_WRITE_CHUNK_SIZE

        if file_size <= CHUNK_SIZE:
            encoded = base64.b64encode(content).decode("utf-8")
            self.send_request(
                "write_file",
                {"path": remote_path, "content": encoded, "append": append},
                timeout=timeout,
            )
            return

        first_chunk = content[:CHUNK_SIZE]
        encoded = base64.b64encode(first_chunk).decode("utf-8")
        self.send_request(
            "write_file",
            {"path": remote_path, "content": encoded, "append": append},
            timeout=timeout,
        )

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
