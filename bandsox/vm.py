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

logger = logging.getLogger(__name__)

# Shared low-level helpers/constants live in vm_common to keep the topical
# mixins free of a circular dependency on this module. Re-exported here so
# `from .vm import kill_process_tree, DEFAULT_KERNEL_PATH, ...` keeps working.
from .vm_common import (  # noqa: E402,F401
    _DIRECT_TEXT_WRITE_MAX_BYTES,
    _SERIAL_WRITE_CHUNK_SIZE,
    _DEBUGFS_FULL_FILE_FALLBACK_LOG_THRESHOLD,
    FastIOError,
    _parse_fastio_error,
    _pid_exists,
    _child_pids,
    _descendant_pids,
    kill_process_tree,
    FIRECRACKER_BIN,
    DEFAULT_KERNEL_PATH,
    DEFAULT_BOOT_ARGS,
)
from .vm_files import _FileOpsMixin


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


class MicroVM(_FileOpsMixin):
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
        self._event_callbacks_lock = threading.Lock()
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

    def _agent_total_lines_for(self, cmd_id: str):
        """Return agent-reported line count for cmd_id, if any."""
        with self._event_callbacks_lock:
            entry = self.event_callbacks.get(cmd_id)
            if entry is None:
                return None
            return entry.get("_agent_total_lines")

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
                with self._event_callbacks_lock:
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
        """Parses a line from stdout (event).

        All event_callbacks accesses are protected by _event_callbacks_lock.
        For "exit" events we atomically pop the entry under the lock so that
        concurrent threads (retry path, vsock fast-path registration,
        _socket_read_loop teardown) can't race on the same cmd_id slot.
        Read-only callbacks are looked up under the lock and invoked outside
        it so a slow callback doesn't stall the dispatch thread.
        """
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
                    with self._event_callbacks_lock:
                        entry = self.event_callbacks.get(cmd_id)
                    if entry:
                        cb = entry.get("on_started")
                        if cb:
                            cb(pid)

                # Generic status dispatch — used by the vsock fast path
                # to notify the caller that the upload already landed on
                # disk (status == "uploaded") so download_file knows not
                # to wait for chunked serial events.
                if cmd_id:
                    with self._event_callbacks_lock:
                        entry = self.event_callbacks.get(cmd_id)
                    if entry:
                        cb = entry.get("on_status")
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
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get(f"on_{stream}")
                    if cb:
                        try:
                            cb(data)
                        except Exception:
                            pass  # Don't let callback crash the loop

            elif evt_type == "file_content":
                cmd_id = payload.get("cmd_id")
                content = payload.get("content")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                    if entry is not None:
                        total_lines = payload.get("total_lines")
                        if total_lines is not None:
                            entry["_agent_total_lines"] = total_lines
                        cb = entry.get("on_file_content")
                    else:
                        cb = None
                if cb:
                    cb(content)

            elif evt_type == "dir_list":
                cmd_id = payload.get("cmd_id")
                files = payload.get("files")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_dir_list")
                    if cb:
                        cb(files)

            elif evt_type == "file_info":
                cmd_id = payload.get("cmd_id")
                info = payload.get("info")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_file_info")
                    if cb:
                        cb(info)

            elif evt_type == "file_chunk":
                cmd_id = payload.get("cmd_id")
                data = payload.get("data")
                offset = payload.get("offset")
                size = payload.get("size")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_file_chunk")
                    if cb:
                        cb(data, offset, size)

            elif evt_type == "file_complete":
                cmd_id = payload.get("cmd_id")
                total_size = payload.get("total_size")
                checksum = payload.get("checksum")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                    if entry is not None:
                        total_lines = payload.get("total_lines")
                        if total_lines is not None:
                            entry["_agent_total_lines"] = total_lines
                        cb = entry.get("on_file_complete")
                    else:
                        cb = None
                if cb:
                    cb(total_size, checksum)

            elif evt_type == "exit":
                cmd_id = payload.get("cmd_id")
                exit_code = payload.get("exit_code")
                # Atomically take ownership so no other thread can touch
                # this cmd_id's entry after we remove it.
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.pop(cmd_id, None)
                if entry:
                    payload_cb = entry.get("on_exit_payload")
                    if payload_cb:
                        payload_cb(payload)
                    cb = entry.get("on_exit")
                    if cb:
                        cb(exit_code)

            elif evt_type == "error":
                cmd_id = payload.get("cmd_id")
                error = payload.get("error")
                logger.error(f"Agent error for cmd {cmd_id}: {error}")
                with self._event_callbacks_lock:
                    entry = self.event_callbacks.get(cmd_id)
                if entry:
                    cb = entry.get("on_error")
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

        with self._event_callbacks_lock:
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

        def _drop_callback():
            with self._event_callbacks_lock:
                self.event_callbacks.pop(cmd_id, None)

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
            _drop_callback()
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
            with self._event_callbacks_lock:
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
                _drop_callback()
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
        _prealloc_network: dict = None,
        _prealloc_vsock: dict = None,
    ):
        """Configures the VM resources.

        When _prealloc_network / _prealloc_vsock are provided, those
        pre-computed configs are applied directly (no allocation loop).
        """
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
        try:
            self.client.put_entropy()
        except Exception as e:
            logger.warning(f"Failed to configure entropy device: {e}")

        # --- Networking ---
        network_config = _prealloc_network
        if network_config is None and enable_networking:
            base_idx = int(self.vm_id[-2:], 16)
            for i in range(50):
                subnet_idx = (base_idx + i) % 253 + 1
                host_ip = f"172.16.{subnet_idx}.1"
                guest_ip = f"172.16.{subnet_idx}.2"
                guest_mac = f"AA:FC:00:00:{subnet_idx:02x}:02"
                host_mac = derive_host_mac(host_ip)

                try:
                    setup_tap_device(self.tap_name, host_ip, host_mac=host_mac)
                    network_config = {
                        "host_ip": host_ip,
                        "guest_ip": guest_ip,
                        "guest_mac": guest_mac,
                        "host_mac": host_mac,
                        "tap_name": self.tap_name,
                    }
                    break
                except Exception:
                    continue
            else:
                raise Exception("Failed to allocate free network subnet after retries")

        if network_config:
            host_ip = network_config["host_ip"]
            guest_ip = network_config["guest_ip"]
            guest_mac = network_config["guest_mac"]
            host_mac = network_config.get("host_mac")
            tap_name = network_config.get("tap_name", self.tap_name)

            if _prealloc_network:
                setup_tap_device(tap_name, host_ip, host_mac=host_mac)

            self.network_config = network_config
            self.network_setup = True
            self.tap_name = tap_name
            self.client.put_network_interface("eth0", tap_name, guest_mac)

            network_boot_args = (
                f"ip={guest_ip}::{host_ip}:255.255.255.0::eth0:off:8.8.8.8"
            )
            boot_args = f"{boot_args} {network_boot_args}"

        self.client.put_boot_source(kernel_path, boot_args)

        # --- Vsock ---
        if _prealloc_vsock and _prealloc_vsock.get("enabled"):
            self._setup_vsock_bridge(
                _prealloc_vsock["cid"], _prealloc_vsock["port"]
            )
        elif enable_vsock:
            from .core import BandSox

            bs = BandSox()
            cid = bs._allocate_cid()
            port = bs._allocate_port()
            self._setup_vsock_bridge(cid, port)

    def configure_prealloc(self, config: dict):
        """Configure VM from a pre-allocated config dict (used by runner).

        Delegates to configure() with pre-computed network/vsock values so
        that Firecracker API calls are not duplicated.
        """
        network_config = config.get("network_config")
        vsock_config = config.get("vsock_config")

        self.configure(
            config["kernel_path"],
            config["rootfs_path"],
            config["vcpu"],
            config["mem_mib"],
            enable_networking=False,
            enable_vsock=False,
            disk_bandwidth_mbps=config.get("disk_bandwidth_mbps", 0),
            disk_iops=config.get("disk_iops", 0),
            _prealloc_network=network_config,
            _prealloc_vsock=vsock_config,
        )

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

