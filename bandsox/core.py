import os
import subprocess
import uuid
import logging
import shutil
import json
import threading
from pathlib import Path
from .vm import MicroVM, DEFAULT_KERNEL_PATH
from .image import build_rootfs
from .network import setup_tap_device, cleanup_tap_device
import time

logger = logging.getLogger(__name__)


class BandSox:
    def __init__(self, storage_dir: str = "/var/lib/bandsox"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir = self.storage_dir / "images"
        self.images_dir.mkdir(exist_ok=True)
        self.snapshots_dir = self.storage_dir / "snapshots"
        self.snapshots_dir.mkdir(exist_ok=True)
        self.sockets_dir = self.storage_dir / "sockets"
        self.sockets_dir.mkdir(exist_ok=True)
        self.metadata_dir = self.storage_dir / "metadata"
        self.metadata_dir.mkdir(exist_ok=True)

        self.active_vms = {}  # vm_id -> MicroVM instance

        self.cid_allocator_path = self.storage_dir / "cid_allocator.json"
        self.port_allocator_path = self.storage_dir / "port_allocator.json"

        if not self.cid_allocator_path.exists():
            with open(self.cid_allocator_path, "w") as f:
                json.dump({"free_cids": [], "next_cid": 3}, f)

        if not self.port_allocator_path.exists():
            with open(self.port_allocator_path, "w") as f:
                json.dump({"next_port": 9000, "used_ports": []}, f)

        # Ensure kernel exists or warn with remediation steps
        if not os.path.exists(DEFAULT_KERNEL_PATH):
            logger.warning(
                f"Kernel not found at {DEFAULT_KERNEL_PATH}. "
                "Run 'bandsox init' (or copy a vmlinux file here) before creating VMs."
            )

    def _save_metadata(self, vm_id: str, metadata: dict):
        import json

        with open(self.metadata_dir / f"{vm_id}.json", "w") as f:
            json.dump(metadata, f)

    def _get_metadata(self, vm_id: str) -> dict:
        import json

        meta_path = self.metadata_dir / f"{vm_id}.json"
        if meta_path.exists():
            with open(meta_path, "r") as f:
                return json.load(f)
        return {}

    def _clone_rootfs(self, src: Path, dest: Path) -> str:
        """
        Clone a rootfs file, preferring reflink/CoW to avoid large copies.
        Returns the method used for logging/debugging.
        """
        start = time.time()
        src = Path(src)
        dest = Path(dest)
        if dest.exists():
            dest.unlink()

        method = "copy"
        try:
            subprocess.run(
                ["cp", "--reflink=always", "--sparse=auto", str(src), str(dest)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            method = "reflink"
        except Exception as e:
            logger.debug(f"Reflink clone failed ({e}); falling back to full copy")
            shutil.copy2(src, dest)
            method = "copy"

        elapsed = time.time() - start
        logger.info(f"Cloned rootfs to {dest.name} via {method} in {elapsed:.2f}s")
        return method

    def _allocate_cid(self) -> int:
        """Allocates a unique CID for a VM using free-list approach."""
        with open(self.cid_allocator_path, "r") as f:
            state = json.load(f)

        free_cids = state.get("free_cids", [])
        if free_cids:
            cid = free_cids.pop(0)
            state["free_cids"] = free_cids
        else:
            cid = state["next_cid"]
            state["next_cid"] = cid + 1

        with open(self.cid_allocator_path, "w") as f:
            json.dump(state, f)

        logger.debug(f"Allocated CID: {cid}")
        return cid

    def _release_cid(self, cid: int):
        """Releases a CID back to the pool using free-list."""
        logger.debug(f"Released CID: {cid}")
        with open(self.cid_allocator_path, "r") as f:
            state = json.load(f)

        if "free_cids" not in state:
            state["free_cids"] = []

        if cid not in state["free_cids"] and cid >= 3:
            state["free_cids"].append(cid)
            state["free_cids"].sort()

        with open(self.cid_allocator_path, "w") as f:
            json.dump(state, f)

    def _allocate_port(self) -> int:
        """Allocates a unique port for vsock communication."""
        with open(self.port_allocator_path, "r") as f:
            state = json.load(f)

        # Allocate next port and add to used_ports
        port = state["next_port"]
        if port < 10000:
            state["next_port"] = port + 1
        else:
            # Wrap around to 9000
            port = 9000
            state["next_port"] = 9001

        # Track this port as in-use
        if "used_ports" not in state:
            state["used_ports"] = []
        state["used_ports"].append(port)

        with open(self.port_allocator_path, "w") as f:
            json.dump(state, f)

        logger.debug(f"Allocated port: {port}")
        return port

    def _release_port(self, port: int):
        """Releases a port back to the pool."""
        with open(self.port_allocator_path, "r") as f:
            state = json.load(f)

        if "used_ports" in state and port in state["used_ports"]:
            state["used_ports"].remove(port)
            with open(self.port_allocator_path, "w") as f:
                json.dump(state, f)
            logger.debug(f"Released port: {port}")

    def _check_vsock_compatibility(self, vm_id: str):
        """Check if VM metadata has vsock_config for compatibility.

        Args:
            vm_id: VM ID to check

        Raises:
            Exception: If VM doesn't have vsock_config (old VM)
        """
        meta = self._get_metadata(vm_id)
        if not meta.get("vsock_config"):
            raise Exception(
                f"VM '{vm_id}' requires vsock support. "
                "This VM was created before vsock was enabled. "
                "Please recreate the VM using the create command. "
                "See VSOCK_MIGRATION.md for detailed migration instructions."
            )
        return meta

    def update_vm_status(self, vm_id: str, status: str):
        """Updates the status field in the VM metadata."""
        meta = self._get_metadata(vm_id)
        if meta:
            meta["status"] = status
            self._save_metadata(vm_id, meta)

    def create_vm(
        self,
        docker_image: str,
        name: str = None,
        vcpu: int = 1,
        mem_mib: int = 128,
        kernel_path: str = DEFAULT_KERNEL_PATH,
        enable_networking: bool = True,
        enable_vsock: bool = True,
        force_rebuild: bool = False,
        disk_size_mib: int = 4096,
        env_vars: dict = None,
        metadata: dict = None,
    ) -> MicroVM:
        """Creates and starts a new VM from a Docker image."""
        vm_id = str(uuid.uuid4())
        logger.info(f"Creating VM {vm_id} from {docker_image}")

        # Validate kernel presence up front for a clearer error
        if not os.path.exists(kernel_path):
            raise FileNotFoundError(
                f"Kernel not found at {kernel_path}. "
                f"Run 'bandsox init --kernel-output {kernel_path}' or copy a vmlinux "
                "from the current directory to this path before creating VMs."
            )

        # 1. Build Rootfs
        sanitized_name = docker_image.replace(":", "_").replace("/", "_")
        base_rootfs = self.images_dir / f"{sanitized_name}.ext4"

        if force_rebuild or not base_rootfs.exists():
            build_rootfs(docker_image, str(base_rootfs))

        # Copy to instance specific path
        instance_rootfs = self.images_dir / f"{vm_id}.ext4"
        self._clone_rootfs(base_rootfs, instance_rootfs)

        # Resize if needed
        # Check current size
        current_size = instance_rootfs.stat().st_size
        target_size = disk_size_mib * 1024 * 1024

        if target_size > current_size:
            logger.info(f"Resizing rootfs from {current_size} to {target_size} bytes")
            try:
                # 1. Extend file
                subprocess.run(
                    ["truncate", "-s", str(target_size), str(instance_rootfs)],
                    check=True,
                )

                # 2. Check filesystem
                subprocess.run(
                    ["e2fsck", "-f", "-p", str(instance_rootfs)], check=False
                )  # -p automatic repair, return code might be non-zero for corrections

                # 3. Resize filesystem
                subprocess.run(["resize2fs", str(instance_rootfs)], check=True)

            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to resize rootfs: {e}")
                # We might continue with original size or fail? Let's warn but continue if possible, or maybe fail is safer.
                # If resize failed, the file might be truncated but FS not resized.
                # e2fsck should fix valid FS but size mismatch might occur.
                # Let's raise to be safe.
                raise Exception(f"Failed to resize disk: {e}")

        # 2. Create VM instance
        socket_path = str(self.sockets_dir / f"{vm_id}.sock")
        vm = ManagedMicroVM(vm_id, socket_path, self)

        # 3. Start Process & Configure
        vm.start_process()
        vm.configure(
            kernel_path,
            str(instance_rootfs),
            vcpu,
            mem_mib,
            enable_networking=enable_networking,
            enable_vsock=enable_vsock,
        )

        if env_vars:
            vm.env_vars = env_vars

        vm.start()

        import time

        vsock_config = None
        if enable_vsock and vm.vsock_enabled:
            vsock_config = {
                "enabled": True,
                "cid": vm.vsock_cid,
                "port": vm.vsock_port,
            }

        self._save_metadata(
            vm_id,
            {
                "id": vm_id,
                "name": name,
                "image": docker_image,
                "vcpu": vcpu,
                "mem_mib": mem_mib,
                "disk_size_mib": disk_size_mib,
                "rootfs_path": str(instance_rootfs),
                "network_config": getattr(vm, "network_config", None),
                "vsock_config": vsock_config,
                "created_at": time.time(),
                "status": "running",
                "pid": vm.process.pid,
                "env_vars": env_vars,
                "metadata": metadata or {},
            },
        )

        self.active_vms[vm_id] = vm
        return vm

    def create_vm_from_dockerfile(
        self,
        dockerfile_path: str,
        tag: str = None,
        name: str = None,
        vcpu: int = 1,
        mem_mib: int = 128,
        disk_size_mib: int = 4096,
        env_vars: dict = None,
        metadata: dict = None,
        **kwargs,
    ) -> MicroVM:
        """Creates a VM from a Dockerfile."""
        if not tag:
            tag = f"bandsox-build-{uuid.uuid4()}"

        from .image import build_image_from_dockerfile

        # Pass force_rebuild to docker build as explicit nocache?
        # Actually kwargs here are for create_vm. 'force_rebuild' in kwargs will be passed to create_vm.
        # But for docker build, we should handle it too.
        nocache = kwargs.get("force_rebuild", False)

        build_image_from_dockerfile(dockerfile_path, tag, nocache=nocache)

        return self.create_vm(
            tag,
            name=name,
            vcpu=vcpu,
            mem_mib=mem_mib,
            disk_size_mib=disk_size_mib,
            env_vars=env_vars,
            metadata=metadata,
            **kwargs,
        )

    def restore_vm(
        self,
        snapshot_id: str,
        name: str = None,
        enable_networking: bool = True,
        detach: bool = True,
        env_vars: dict = None,
        metadata: dict = None,
    ) -> MicroVM:
        """Restores a VM from a snapshot."""
        # Snapshot ID should point to a folder containing snapshot file and mem file
        snap_dir = self.snapshots_dir / snapshot_id
        if not snap_dir.exists():
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found")

        snapshot_path = snap_dir / "snapshot_file"
        mem_path = snap_dir / "mem_file"
        # Backwards-compatibility for any legacy references
        mem_file_path = mem_path

        # Load snapshot metadata to get VM configuration
        import json
        import time

        snapshot_meta = {}
        meta_file = snap_dir / "metadata.json"
        if meta_file.exists():
            with open(meta_file, "r") as f:
                snapshot_meta = json.load(f)

        # We need a new VM ID for the restored instance
        new_vm_id = str(uuid.uuid4())
        socket_path = str(self.sockets_dir / f"{new_vm_id}.sock")

        # Use ManagedMicroVM.create_from_snapshot but we need to inject 'bandsox' instance
        # Since create_from_snapshot is a class method on MicroVM, we can't easily override it to return ManagedMicroVM with extra args
        # So we instantiate manually
        # Prepare network args
        guest_mac = None
        netns_name = None

        if enable_networking:
            net_config = snapshot_meta.get("network_config", {})
            guest_mac = net_config.get("guest_mac")
            old_tap_name = net_config.get("tap_name")

            if net_config and old_tap_name:
                # To robustly restore networking, we must provide the backend device
                # that the snapshot expects (same TAP name).
                # To avoid collisions (Resource busy), we create this TAP device
                # inside a new Network Namespace unique to this VM.
                # We use a rename workaround in setup_netns_networking to avoid "Busy" error.
                from .network import setup_netns_networking

                netns_name = f"netns{new_vm_id[:8]}"
                host_ip = net_config.get("host_ip", "172.16.100.1")

                # Setup NetNS with the OLD tap name
                try:
                    cni_ip = setup_netns_networking(
                        netns_name, old_tap_name, host_ip, new_vm_id
                    )

                    # Add route on Host to Guest via CNI IP
                    # We need the Guest IP. We can infer it from net_config or calculate it
                    # if it was standard. But snapshot config is best.
                    guest_ip = net_config.get("guest_ip")
                    if cni_ip and guest_ip:
                        from .network import add_host_route

                        add_host_route(guest_ip, cni_ip)

                except Exception as e:
                    logger.error(f"Failed to setup netns: {e}")
                    raise e

            else:
                pass

        # Instantiate VM
        vm = ManagedMicroVM(new_vm_id, socket_path, self, netns=netns_name)

        if netns_name:
            vm.tap_name = old_tap_name
            vm.network_config = net_config
            vm.netns = netns_name

        runner_process = None

        if env_vars:
            vm.env_vars = env_vars
        elif "env_vars" in snapshot_meta:
            vm.env_vars = snapshot_meta["env_vars"]

        # Precompute log path for detached runner so we can surface it on failures
        log_dir = self.storage_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{new_vm_id}.log"

        def _ensure_netns():
            """Recreate netns/tap if it was cleaned up between runs (e.g., previous VM shutdown)."""
            if not netns_name:
                return
            netns_path = Path("/var/run/netns") / netns_name
            if netns_path.exists():
                return
            try:
                from .network import setup_netns_networking, add_host_route

                logger.info(f"NetNS {netns_name} missing; recreating before start")
                cni_ip = setup_netns_networking(
                    netns_name, old_tap_name, host_ip, new_vm_id
                )
                guest_ip = net_config.get("guest_ip")
                if cni_ip and guest_ip:
                    add_host_route(guest_ip, cni_ip)
            except Exception as e:
                logger.error(f"Failed to recreate NetNS {netns_name}: {e}")
                raise

        def _start_vm_process():
            nonlocal runner_process
            socket_parent = Path(socket_path).parent
            socket_parent.mkdir(parents=True, exist_ok=True)
            _ensure_netns()
            # Proactively clear any stale socket so Firecracker can bind cleanly
            if os.path.exists(socket_path):
                try:
                    os.unlink(socket_path)
                except PermissionError as e:
                    raise Exception(f"Cannot remove stale socket {socket_path}: {e}")
            if detach:
                import sys
                import subprocess

                runner_cmd = [
                    sys.executable,
                    "-m",
                    "bandsox.runner",
                    new_vm_id,
                    "--socket-path",
                    socket_path,
                ]
                if netns_name:
                    runner_cmd.extend(["--netns", netns_name])

                logger.info(f"Spawning detached runner for VM {new_vm_id}")
                with open(log_file, "w") as f:
                    # We do NOT use start_new_session=True because it breaks sudo (loses tty/tickets).
                    # Instead, the runner ignores SIGINT/SIGHUP to detach logically.
                    runner_process = subprocess.Popen(
                        runner_cmd,
                        stdin=subprocess.DEVNULL,
                        stdout=f,
                        stderr=subprocess.STDOUT,
                    )

                # Wait for API socket to appear, surfacing runner crashes promptly
                start_wait = time.time()
                while True:
                    if os.path.exists(socket_path):
                        break
                    if runner_process and runner_process.poll() is not None:
                        raise Exception(
                            f"Detached runner exited with code {runner_process.returncode}. "
                            f"See log at {log_file}"
                        )
                    if time.time() - start_wait > 20:
                        raise Exception(
                            f"Timeout waiting for detached runner to start Firecracker. "
                            f"See log at {log_file}"
                        )
                    time.sleep(0.1)
            else:
                vm.start_process()

        _start_vm_process()
        # Copy snapshot rootfs if available (must do this before starting process potentially?)
        snap_rootfs = snapshot_meta.get("rootfs_path")
        instance_rootfs = self.images_dir / f"{new_vm_id}.ext4"

        if snap_rootfs and os.path.exists(snap_rootfs):
            self._clone_rootfs(snap_rootfs, instance_rootfs)

        # Handle vsock socket cleanup before loading snapshot
        # Firecracker will try to bind to the old socket path from the snapshot
        vsock_config = snapshot_meta.get("vsock_config")
        if vsock_config and vsock_config.get("enabled"):
            old_vm_id = snapshot_meta.get("source_vm_id")
            if old_vm_id:
                old_socket_path = f"/tmp/bandsox/vsock_{old_vm_id}.sock"

                # Clean up stale vsock socket to avoid "Address in use" error
                if os.path.exists(old_socket_path):
                    try:
                        os.unlink(old_socket_path)
                        logger.debug(f"Removed stale vsock socket: {old_socket_path}")
                    except Exception as e:
                        logger.warning(
                            f"Failed to remove vsock socket {old_socket_path}: {e}"
                        )

                # Tell VM to expect the old socket path (Firecracker will recreate it)
                vm.vsock_socket_path = old_socket_path

        # Try to load snapshot
        created_symlink = None

        try:
            vm.load_snapshot(
                str(snapshot_path),
                str(mem_path),
                enable_networking=enable_networking,
                guest_mac=guest_mac,
            )
        except Exception as e:
            import re

            msg = str(e)

            # Simple regex to catch path at end of string
            # We assume path starts with / and goes to end or "}"
            match = re.search(
                r"(?:No such file or directory|os error 2).*? (/[\w\-/.]+\.ext4)", msg
            )
            if not match:
                # Try matching permission denied too? The user saw os error 2.
                match = re.search(r"(/[\w\-/.]+\.ext4)[^}]*$", msg)

            if match:
                missing_path = Path(match.group(1))
                # Suppress the warning if we are about to fix it, but keep debug log
                logger.debug(
                    f"Snapshot expects missing file: {missing_path}. Creating fallback symlink."
                )

                # Double check we are not overwriting something important
                if not missing_path.exists():
                    try:
                        missing_path.parent.mkdir(parents=True, exist_ok=True)
                        missing_path.symlink_to(instance_rootfs)
                        created_symlink = missing_path

                        # Restart process to ensure clean state
                        logger.info("Restarting Firecracker process for retry...")
                        if detach and runner_process:
                            runner_process.terminate()
                            try:
                                runner_process.wait(timeout=1)
                            except:
                                runner_process.kill()
                            if os.path.exists(socket_path):
                                os.unlink(socket_path)
                        else:
                            vm.stop()

                        _start_vm_process()

                        # Retry load
                        vm.load_snapshot(
                            str(snapshot_path),
                            str(mem_path),
                            enable_networking=enable_networking,
                            guest_mac=guest_mac,
                        )

                        # If success, we MUST update the drive to the new path immediately
                        # to ensure Firecracker uses our new file and we can delete the symlink safely
                        vm.update_drive("rootfs", str(instance_rootfs))

                    except Exception as retry_e:
                        logger.error(
                            f"Failed to recover from missing backing file: {retry_e}"
                        )
                        raise retry_e
                    finally:
                        if created_symlink and created_symlink.is_symlink():
                            created_symlink.unlink()
                else:
                    # File exists but maybe permissions? Or we misidentified.
                    logger.warning(
                        f"File {missing_path} exists, cannot use symlink trick. Error was: {e}"
                    )
                    raise e
            else:
                raise e
        if snap_rootfs and os.path.exists(snap_rootfs):
            # Update rootfs path to the new instance copy (this also frees us from the symlink)
            vm.update_drive("rootfs", str(instance_rootfs))

        # Configure offloading NOW that the VM is attached (fd open)
        if netns_name and old_tap_name:
            from .network import configure_tap_offloading

            configure_tap_offloading(netns_name, old_tap_name, vm.vm_id)

        vm.resume()

        # Re-establish vsock bridge if vsock was enabled in the snapshot
        vsock_config = snapshot_meta.get("vsock_config")
        if vsock_config and vsock_config.get("enabled"):
            old_vm_id = snapshot_meta.get("source_vm_id", new_vm_id)
            old_socket_path = f"/tmp/bandsox/vsock_{old_vm_id}.sock"

            try:
                # Firecracker should have recreated the socket after loading snapshot
                max_wait = 50
                for i in range(max_wait):
                    if os.path.exists(old_socket_path):
                        break
                    time.sleep(0.1)
                else:
                    raise Exception(
                        f"Vsock socket not created after restore: {old_socket_path}"
                    )

                # Reconnect to vsock socket
                import socket

                vm.vsock_bridge_socket = socket.socket(
                    socket.AF_UNIX, socket.SOCK_STREAM
                )
                vm.vsock_bridge_socket.connect(old_socket_path)
                vm.vsock_bridge_socket.settimeout(30)

                # Use SAME CID/port from snapshot (agent already has them)
                vm.vsock_cid = vsock_config["cid"]
                vm.vsock_port = vsock_config["port"]
                vm.vsock_enabled = True
                vm.vsock_bridge_running = True

                # Restart vsock bridge thread
                vm.vsock_bridge_thread = threading.Thread(
                    target=vm._vsock_bridge_loop, daemon=True
                )
                vm.vsock_bridge_thread.start()

                vm.env_vars["BANDSOX_VSOCK_PORT"] = str(vsock_config["port"])
                logger.info(
                    f"Vsock restored: CID={vsock_config['cid']}, port={vsock_config['port']}"
                )
            except Exception as e:
                logger.warning(f"Failed to restore vsock bridge: {e}")
                # VM continues without vsock (falls back to serial)
                vm.vsock_enabled = False

        if enable_networking and vm.agent_ready:
            # Check if we need to update IP
            current_guest_ip = vm.network_config.get("guest_ip")
            old_guest_ip = snapshot_meta.get("network_config", {}).get("guest_ip")

            if current_guest_ip and old_guest_ip and current_guest_ip != old_guest_ip:
                logger.info(
                    f"Reconfiguring Guest IP from {old_guest_ip} to {current_guest_ip}"
                )
                host_ip = vm.network_config.get("host_ip")

                # Wait for agent to be responsive
                try:
                    vm.wait_for_agent(timeout=10)
                    # Flusing ip and adding new one
                    # Note: This might break connectivity temporarily so we chain commands
                    cmd = f"ip addr flush dev eth0; ip addr add {current_guest_ip}/24 dev eth0; ip route add default via {host_ip}"
                    vm.exec_command(cmd)
                except Exception as e:
                    logger.warning(f"Failed to update Guest IP: {e}")

        # Agent is already running in the restored VM
        vm.agent_ready = True

        # Save metadata (inherit from snapshot if possible, or create new)
        self._save_metadata(
            new_vm_id,
            {
                "id": new_vm_id,
                "name": name
                if name
                else f"from-{snapshot_id}",  # Descriptive name for restored VMs
                "image": snapshot_meta.get("image", "snapshot:" + snapshot_id),
                "vcpu": snapshot_meta.get("vcpu", 1),
                "mem_mib": snapshot_meta.get("mem_mib", 128),
                "created_at": time.time(),
                "status": "running",
                "restored_from": snapshot_id,
                "rootfs_path": str(instance_rootfs),
                "network_config": vm.network_config
                if hasattr(vm, "network_config")
                else None,
                "pid": runner_process.pid
                if detach and runner_process
                else vm.process.pid
                if vm.process
                else None,
                "agent_ready": True,
                "env_vars": vm.env_vars,
                "metadata": metadata
                if metadata is not None
                else snapshot_meta.get("metadata", {}),
                "vsock_config": vsock_config,
            },
        )

        self.active_vms[new_vm_id] = vm
        return vm

    def snapshot_vm(
        self, vm: MicroVM, snapshot_name: str = None, metadata: dict = None
    ) -> str:
        """Snapshot a VM without changing its pre-snapshot running/paused state."""
        if not snapshot_name:
            snapshot_name = (
                f"{vm.vm_id}_{int(os.path.getmtime(vm.socket_path))}"  # timestampish
            )

        snap_dir = self.snapshots_dir / snapshot_name
        snap_dir.mkdir(exist_ok=True)

        snapshot_path = snap_dir / "snapshot_file"
        mem_path = snap_dir / "mem_file"

        meta = self._get_metadata(vm.vm_id) or {}
        was_paused = meta.get("status") == "paused"

        # Disconnect vsock bridge before snapshot to avoid "Address in use" error on restore
        # Firecracker saves vsock device state to snapshot file, so we need to release the socket
        had_vsock = vm.vsock_enabled and vm.vsock_bridge_running
        if had_vsock:
            logger.info(f"Disconnecting vsock bridge before snapshot for {vm.vm_id}")
            vm._cleanup_vsock_bridge()
            # Don't reconnect after snapshot - let guest re-establish vsock connections
            vm.vsock_enabled = False

        # Pause VM if it was running; keep paused VMs paused after snapshot
        if not was_paused:
            vm.pause()

        try:
            vm.snapshot(str(snapshot_path), str(mem_path))
        finally:
            if not was_paused:
                # Only resume if we paused it
                vm.resume()

        # Save snapshot metadata including VM configuration
        import json
        import shutil

        # Copy rootfs to snapshot directory
        vm_meta = self._get_metadata(vm.vm_id)
        source_rootfs = Path(vm_meta.get("rootfs_path"))
        snap_rootfs = snap_dir / "rootfs.ext4"
        if source_rootfs.exists():
            self._clone_rootfs(source_rootfs, snap_rootfs)

        snapshot_meta = {
            "snapshot_name": snapshot_name,
            "source_vm_id": vm.vm_id,
            "vcpu": vm_meta.get("vcpu", 1),
            "mem_mib": vm_meta.get("mem_mib", 128),
            "image": vm_meta.get("image", "unknown"),
            "rootfs_path": str(snap_rootfs),  # Point to the snapshot copy
            "backend_rootfs_path": str(
                source_rootfs
            ),  # Original path for reference/symlink matching
            "network_config": vm_meta.get("network_config"),
            "metadata": metadata
            if metadata is not None
            else vm_meta.get("metadata", {}),
            "created_at": os.path.getmtime(str(snapshot_path))
            if os.path.exists(str(snapshot_path))
            else None,
        }
        with open(snap_dir / "metadata.json", "w") as f:
            json.dump(snapshot_meta, f)

        return snapshot_name

    def delete_snapshot(self, snapshot_id: str):
        """Deletes a snapshot."""
        snap_dir = self.snapshots_dir / snapshot_id
        if snap_dir.exists() and snap_dir.is_dir():
            import shutil

            shutil.rmtree(snap_dir)
        else:
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found")

    def update_snapshot_metadata(self, snapshot_id: str, metadata: dict) -> dict:
        """Updates the metadata of a snapshot."""
        import json

        snap_dir = self.snapshots_dir / snapshot_id
        if not snap_dir.exists() or not snap_dir.is_dir():
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found")

        meta_file = snap_dir / "metadata.json"
        if not meta_file.exists():
            raise FileNotFoundError(f"Snapshot metadata not found for {snapshot_id}")

        # Load current snapshot metadata
        with open(meta_file, "r") as f:
            snapshot_meta = json.load(f)

        # Update the metadata field
        snapshot_meta["metadata"] = metadata

        # Save back to file
        with open(meta_file, "w") as f:
            json.dump(snapshot_meta, f)

        return snapshot_meta

    def rename_snapshot(self, snapshot_id: str, new_name: str):
        """Renames a snapshot (updates only snapshot_name field, preserving snapshot_id and directory)."""
        import json

        snap_dir = self.snapshots_dir / snapshot_id
        if not snap_dir.exists() or not snap_dir.is_dir():
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found")

        meta_file = snap_dir / "metadata.json"
        if not meta_file.exists():
            raise FileNotFoundError(f"Snapshot metadata not found for {snapshot_id}")

        # Load current snapshot metadata
        with open(meta_file, "r") as f:
            snapshot_meta = json.load(f)

        # Update the snapshot_name field
        snapshot_meta["snapshot_name"] = new_name

        # Save back to file
        with open(meta_file, "w") as f:
            json.dump(snapshot_meta, f)

        logger.info(f"Snapshot {snapshot_id} renamed to '{new_name}'")

    def list_vms(self, limit: int = None, metadata_equals: dict = None):
        """Lists all VMs (running and stopped)."""
        vms = []
        for meta_file in self.metadata_dir.glob("*.json"):
            import json

            try:
                with open(meta_file, "r") as f:
                    meta = json.load(f)

                vm_id = meta.get("id")
                socket_path = self.sockets_dir / f"{vm_id}.sock"

                if socket_path.exists():
                    pass  # Running
                else:
                    # Socket missing, assume stopped
                    if meta.get("status") != "stopped":
                        meta["status"] = "stopped"

                # Filtering (only append if matches)
                if metadata_equals:
                    vm_meta = meta.get("metadata", {})
                    match = True
                    for k, v in metadata_equals.items():
                        if vm_meta.get(k) != v:
                            match = False
                            break
                    if not match:
                        continue

                vms.append(meta)
            except Exception:
                pass

        # Sort by created_at desc to make limit meaningful
        vms.sort(key=lambda x: x.get("created_at", 0), reverse=True)

        if limit is not None:
            vms = vms[:limit]

        return vms

    def get_vm_info(self, vm_id: str):
        """Gets detailed information about a specific VM."""
        meta = self._get_metadata(vm_id)
        if not meta:
            return None

        socket_path = self.sockets_dir / f"{vm_id}.sock"
        if not socket_path.exists() and meta.get("status") != "stopped":
            meta["status"] = "stopped"

        return meta

    def update_vm_metadata(self, vm_id: str, metadata: dict):
        """Updates the metadata of a VM."""
        meta = self._get_metadata(vm_id)
        if not meta:
            raise FileNotFoundError(f"VM {vm_id} not found")

        meta["metadata"] = metadata
        self._save_metadata(vm_id, meta)

    def rename_vm(self, vm_id: str, new_name: str):
        """Renames a VM (updates only the name field, preserving vm_id)."""
        meta = self._get_metadata(vm_id)
        if not meta:
            raise FileNotFoundError(f"VM {vm_id} not found")

        meta["name"] = new_name
        self._save_metadata(vm_id, meta)
        logger.info(f"VM {vm_id} renamed to '{new_name}'")

    def list_snapshots(self):
        """Lists all snapshots."""
        snapshots = []
        for snap_dir in self.snapshots_dir.iterdir():
            if snap_dir.is_dir():
                # Try to load metadata.json from the snapshot directory
                meta_file = snap_dir / "metadata.json"
                if meta_file.exists():
                    import json

                    try:
                        with open(meta_file, "r") as f:
                            meta = json.load(f)
                        # Ensure id exists
                        if "id" not in meta:
                            meta["id"] = meta.get("snapshot_name", snap_dir.name)

                        # Ensure path exists
                        meta["path"] = str(snap_dir)

                        snapshots.append(meta)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Could not decode metadata for snapshot {snap_dir.name}"
                        )
                        snapshots.append(
                            {
                                "id": snap_dir.name,
                                "path": str(snap_dir),
                                "status": "metadata_corrupted",
                            }
                        )
                else:
                    snapshots.append(
                        {
                            "id": snap_dir.name,
                            "path": str(snap_dir),
                            "status": "no_metadata",
                        }
                    )
        return snapshots

    def delete_vm(self, vm_id: str):
        """Deletes a VM and its resources."""
        # Check if VM exists (metadata)
        meta_path = self.metadata_dir / f"{vm_id}.json"
        if not meta_path.exists():
            logger.warning(f"Attempted to delete non-existent VM: {vm_id}")
            return

        # 1. Try to stop if running (ignore errors)
        try:
            vm = self.get_vm(vm_id)
            if vm:
                vm.stop()
        except Exception:
            pass

        # 2. Delete socket
        socket_path = self.sockets_dir / f"{vm_id}.sock"
        if socket_path.exists():
            socket_path.unlink()

        # 3. Delete metadata
        if meta_path.exists():
            meta_path.unlink()

        # 4. Delete instance rootfs
        rootfs_path = self.images_dir / f"{vm_id}.ext4"
        if rootfs_path.exists():
            rootfs_path.unlink()

        if vm_id in self.active_vms:
            del self.active_vms[vm_id]

    def get_vm(self, vm_id: str) -> MicroVM:
        """Gets a running VM instance by ID."""
        if vm_id in self.active_vms:
            return self.active_vms[vm_id]

        socket_path = self.sockets_dir / f"{vm_id}.sock"
        if not socket_path.exists():
            return None

        # If we are here, it means the VM is running (socket exists) but not in our memory.
        # This happens if the server restarted or if another process started the VM.
        # We can create a ManagedMicroVM, but it won't have the process handle.
        # This limits functionality (no stdin/stdout access).
        vm = ManagedMicroVM(vm_id, str(socket_path), self)

        # Populate rootfs_path from metadata if available
        meta = self._get_metadata(vm_id)
        if meta and "rootfs_path" in meta:
            vm.rootfs_path = meta["rootfs_path"]

        if meta and "network_config" in meta:
            vm.network_config = meta["network_config"]

        if meta and "env_vars" in meta:
            vm.env_vars = meta["env_vars"]

        return vm


class ManagedMicroVM(MicroVM):
    def __init__(
        self, vm_id: str, socket_path: str, bandsox: "BandSox", netns: str = None
    ):
        super().__init__(vm_id, socket_path, netns=netns)
        self.bandsox = bandsox

    def _handle_stdout_line(self, line):
        """Override to intercept status events."""
        super()._handle_stdout_line(line)

        # Check if we are ready
        # We can't rely just on super() setting self.agent_ready because that's in-memory only
        # and this instance might be ephemeral or the server might be looking at a different instance.
        # But wait, super()._handle_stdout_line calls self.agent_ready = True.

        # We need to detect when it BECOMES ready to update metadata
        if self.agent_ready:
            # Check if metadata already says running/ready?
            # We just blindly update for now if it's not marked as ready?
            # actually "status": "running" is general VM status.
            # We might need a specific field "agent_ready": true

            # Optimization: don't write to disk on every line.
            # super() parses the JSON. We should intercept the parsing result?
            # But _handle_stdout_line does everything.

            # Let's just parse it again or check if agent_ready changed?
            # No, easier to just check if the line was the ready event.
            if '"status": "ready"' in line or '"status": "ready"' in line.replace(
                " ", ""
            ):
                meta = self.bandsox._get_metadata(self.vm_id)
                if not meta.get("agent_ready"):
                    meta["agent_ready"] = True
                    self.bandsox._save_metadata(self.vm_id, meta)

    def pause(self):
        # Check if already paused
        meta = self.bandsox._get_metadata(self.vm_id)
        if meta.get("status") == "paused":
            logger.warning(f"Attempted to pause already paused VM: {self.vm_id}")
            return

        try:
            super().pause()
            self.bandsox.update_vm_status(self.vm_id, "paused")
        except Exception as e:
            # Check for connection error indicating VM is gone
            if "Connection refused" in str(e) or isinstance(e, FileNotFoundError):
                logger.warning(
                    f"Attempted to pause non-existent/deleted VM: {self.vm_id}"
                )
                raise e
            raise e

    def resume(self):
        try:
            super().resume()
            self.bandsox.update_vm_status(self.vm_id, "running")
        except Exception as e:
            if "Connection refused" in str(e) or isinstance(e, FileNotFoundError):
                logger.warning(
                    f"Attempted to resume non-existent/deleted VM: {self.vm_id}"
                )
                raise e
            raise e

    def stop(self):
        if self.vsock_enabled:
            if hasattr(self, "bandsox") and self.bandsox:
                if self.vsock_cid:
                    self.bandsox._release_cid(self.vsock_cid)
                if self.vsock_port:
                    self.bandsox._release_port(self.vsock_port)

        meta = self.bandsox._get_metadata(self.vm_id)
        pid = meta.get("pid")

        if pid:
            import signal

            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                logger.error(f"Permission denied killing PID {pid}")

        super().stop()
        self.bandsox.update_vm_status(self.vm_id, "stopped")

        meta = self.bandsox._get_metadata(self.vm_id)
        if meta.get("agent_ready"):
            meta["agent_ready"] = False
            self.bandsox._save_metadata(self.vm_id, meta)

    def wait_for_agent(self, timeout=30):
        """Waits for the agent to be ready and connected."""
        start = time.time()
        while time.time() - start < timeout:
            # 1. Ensure connection
            if not self.process and not self.console_conn:
                try:
                    self.connect_to_console()
                except Exception:
                    pass  # connection might fail if socket not ready yet

            # 2. Check if process died (if we own it)
            if self.process and self.process.poll() is not None:
                raise Exception(
                    f"VM process exited unexpectedly with code {self.process.returncode}"
                )

            # 3. Check readiness
            # If we don't have a connection yet, we are not ready to return,
            # even if metadata says ready (because we need to send data).
            if self.process or self.console_conn:
                if self.agent_ready:
                    return True

                # Check metadata as fallback
                meta = self.bandsox._get_metadata(self.vm_id)
                if meta.get("agent_ready"):
                    self.agent_ready = True
                    return True

            time.sleep(0.5)

        return False

    def start_pty_session(self, *args, **kwargs):
        if not self.wait_for_agent():
            raise Exception("Agent not ready")
        return super().start_pty_session(*args, **kwargs)

    def exec_command(self, *args, **kwargs):
        if not self.wait_for_agent():
            raise Exception("Agent not ready")
        return super().exec_command(*args, **kwargs)

    def exec_python(self, *args, **kwargs):
        if not self.wait_for_agent():
            raise Exception("Agent not ready")
        return super().exec_python(*args, **kwargs)

    def exec_python_capture(self, *args, **kwargs):
        if not self.wait_for_agent():
            raise Exception("Agent not ready")
        return super().exec_python_capture(*args, **kwargs)

    def delete(self):
        self.bandsox.delete_vm(self.vm_id)
