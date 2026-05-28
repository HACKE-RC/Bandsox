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
from .vm_vsock_io import _VsockIOMixin
from .vm_exec import _ExecMixin
from .vm_agent_transport import _AgentTransportMixin


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


class MicroVM(_FileOpsMixin, _VsockIOMixin, _ExecMixin, _AgentTransportMixin):
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

