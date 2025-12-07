import os
import uuid
import logging
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
        
        self.active_vms = {} # vm_id -> MicroVM instance
        
        # Ensure kernel exists or warn
        if not os.path.exists(DEFAULT_KERNEL_PATH):
            logger.warning(f"Kernel not found at {DEFAULT_KERNEL_PATH}. VMs may fail to start.")

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

    def update_vm_status(self, vm_id: str, status: str):
        """Updates the status field in the VM metadata."""
        meta = self._get_metadata(vm_id)
        if meta:
            meta["status"] = status
            self._save_metadata(vm_id, meta)


    def _inject_agent(self, rootfs_path: str):
        """Injects the current agent.py into the rootfs."""
        agent_path = Path(os.path.dirname(__file__)) / "agent.py"
        if not agent_path.exists():
            logger.warning("Agent script not found, cannot inject.")
            return

        import subprocess
        try:
            # Remove existing agent
            # We don't check output as it might not exist
            subprocess.run(["debugfs", "-w", "-R", "rm /usr/local/bin/agent.py", rootfs_path], capture_output=True)
            
            # Write new agent
            cmd = ["debugfs", "-w", "-R", f"write {agent_path} /usr/local/bin/agent.py", rootfs_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                logger.error(f"Failed to inject agent: {result.stderr}")
            else:
                # Set permissions? debugfs write creates with root owner 0644 or similar?
                # We need it executable 0755
                # debugfs doesn't support chmod easily? 
                # It copies mode from source? No.
                # debugfs 'sif' set inode field? 'sif /usr/local/bin/agent.py mode 0100755'
                subprocess.run(["debugfs", "-w", "-R", "sif /usr/local/bin/agent.py mode 0100755", rootfs_path], capture_output=True)
                
                logger.debug(f"Injected agent into {rootfs_path}")
        except Exception as e:
            logger.error(f"Failed to inject agent: {e}")

    def create_vm(self, docker_image: str, name: str = None, vcpu: int = 1, mem_mib: int = 128, kernel_path: str = DEFAULT_KERNEL_PATH, enable_networking: bool = True, force_rebuild: bool = False) -> MicroVM:
        """Creates and starts a new VM from a Docker image."""
        vm_id = str(uuid.uuid4())
        logger.info(f"Creating VM {vm_id} from {docker_image}")
        
        # 1. Build Rootfs
        sanitized_name = docker_image.replace(":", "_").replace("/", "_")
        base_rootfs = self.images_dir / f"{sanitized_name}.ext4"
        
        if force_rebuild or not base_rootfs.exists():
            build_rootfs(docker_image, str(base_rootfs))
            
        # Copy to instance specific path
        instance_rootfs = self.images_dir / f"{vm_id}.ext4"
        import shutil
        shutil.copy2(base_rootfs, instance_rootfs)
        
        # Inject latest agent
        self._inject_agent(str(instance_rootfs))
        
        # 2. Create VM instance
        socket_path = str(self.sockets_dir / f"{vm_id}.sock")
        vm = ManagedMicroVM(vm_id, socket_path, self)
        
        # 3. Start Process & Configure
        vm.start_process()
        vm.configure(kernel_path, str(instance_rootfs), vcpu, mem_mib, enable_networking=enable_networking)
        
        # 4. Start VM
        vm.start()
        
        # Save metadata
        import time
        self._save_metadata(vm_id, {
            "id": vm_id,
            "name": name,  # Store name as-is, None if not provided
            "image": docker_image,
            "vcpu": vcpu,
            "mem_mib": mem_mib,
            "rootfs_path": str(instance_rootfs),  # Save rootfs path for file operations
            "network_config": getattr(vm, "network_config", None),
            "created_at": time.time(),
            "status": "running",
            "pid": vm.process.pid
        })
        
        self.active_vms[vm_id] = vm
        return vm

    def create_vm_from_dockerfile(self, dockerfile_path: str, tag: str = None, name: str = None, vcpu: int = 1, mem_mib: int = 128, **kwargs) -> MicroVM:
        """Creates a VM from a Dockerfile."""
        if not tag:
            tag = f"bandsox-build-{uuid.uuid4()}"
            
        from .image import build_image_from_dockerfile
        # Pass force_rebuild to docker build as explicit nocache? 
        # Actually kwargs here are for create_vm. 'force_rebuild' in kwargs will be passed to create_vm.
        # But for docker build, we should handle it too.
        nocache = kwargs.get('force_rebuild', False)
        
        build_image_from_dockerfile(dockerfile_path, tag, nocache=nocache)
        
        return self.create_vm(tag, name=name, vcpu=vcpu, mem_mib=mem_mib, **kwargs)

    def restore_vm(self, snapshot_id: str, name: str = None, enable_networking: bool = True) -> MicroVM:
        """Restores a VM from a snapshot."""
        # Snapshot ID should point to a folder containing snapshot file and mem file
        snap_dir = self.snapshots_dir / snapshot_id
        if not snap_dir.exists():
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found")
            
        snapshot_path = snap_dir / "snapshot_file"
        mem_path = snap_dir / "mem_file"
        
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
        vm = ManagedMicroVM(new_vm_id, socket_path, self)
        vm.start_process()
        
        # Copy snapshot rootfs if available
        import shutil
        snap_rootfs = snapshot_meta.get("rootfs_path")
        instance_rootfs = self.images_dir / f"{new_vm_id}.ext4"
        
        if snap_rootfs and os.path.exists(snap_rootfs):
            shutil.copy2(snap_rootfs, instance_rootfs)
            # Inject latest agent
            self._inject_agent(str(instance_rootfs))
            # We need to tell Firecracker to use this new rootfs
            # We do this AFTER load_snapshot but BEFORE resume
        else:
            # Fallback to image if snapshot rootfs missing (legacy snapshots)
            # This might fail if the original image is gone, but it's the best we can do
            logger.warning(f"Snapshot {snapshot_id} missing rootfs_path, trying to recover...")
            # We can't easily recover if we don't know what the backing file was.
            # But if we assume the snapshot points to a file that exists, we are fine.
            # If not, we fail.
            pass

        # Try to load snapshot. If backing file is missing (common with snapshots from deleted VMs),
        # we try to create a symlink at the expected location pointing to our new instance_rootfs.
        created_symlink = None
        
        # Prepare network args
        guest_mac = None
        if enable_networking:
            net_config = snapshot_meta.get("network_config", {})
            guest_mac = net_config.get("guest_mac")

        try:
            vm.load_snapshot(str(snapshot_path), str(mem_path), enable_networking=enable_networking, guest_mac=guest_mac)
        except Exception as e:
            # Check if error is due to missing backing file
            # Error format: ... No such file or directory (os error 2) /path/to/file.ext4 ...
            import re
            msg = str(e)
            match = re.search(r"No such file or directory \(os error 2\) ([^\"]+)", msg)
            if match:
                missing_path = Path(match.group(1))
                logger.warning(f"Snapshot expects missing file: {missing_path}. Creating fallback symlink.")
                
                if not missing_path.exists():
                    try:
                        missing_path.parent.mkdir(parents=True, exist_ok=True)
                        missing_path.symlink_to(instance_rootfs)
                        created_symlink = missing_path
                        
                        # Restart process to ensure clean state
                        logger.info("Restarting Firecracker process for retry...")
                        vm.stop()
                        vm.start_process()
                        
                        # Retry load
                        vm.load_snapshot(str(snapshot_path), str(mem_path), enable_networking=enable_networking, guest_mac=guest_mac)
                    except Exception as retry_e:
                        logger.error(f"Failed to recover from missing backing file: {retry_e}")
                        if created_symlink and created_symlink.exists():
                             created_symlink.unlink()
                        raise retry_e
            else:
                 raise e
        
        if snap_rootfs and os.path.exists(snap_rootfs):
            # Update rootfs path to the new instance copy (this also frees us from the symlink)
            vm.update_drive("rootfs", str(instance_rootfs))
            
        if created_symlink and created_symlink.exists():
            created_symlink.unlink()

        vm.resume()
        
        if enable_networking and vm.agent_ready: 
             # Check if we need to update IP
             current_guest_ip = vm.network_config.get("guest_ip")
             old_guest_ip = snapshot_meta.get("network_config", {}).get("guest_ip")
             
             if current_guest_ip and old_guest_ip and current_guest_ip != old_guest_ip:
                 logger.info(f"Reconfiguring Guest IP from {old_guest_ip} to {current_guest_ip}")
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
        self._save_metadata(new_vm_id, {
            "id": new_vm_id,
            "name": name if name else f"from-{snapshot_id}",  # Descriptive name for restored VMs
            "image": snapshot_meta.get("image", "snapshot:" + snapshot_id),
            "vcpu": snapshot_meta.get("vcpu", 1),
            "mem_mib": snapshot_meta.get("mem_mib", 128),
            "created_at": time.time(),
            "status": "running",
            "restored_from": snapshot_id,
            "rootfs_path": str(instance_rootfs),
            "network_config": vm.network_config if hasattr(vm, "network_config") else None,
            "pid": vm.process.pid,
            "agent_ready": True
        })
        
        self.active_vms[new_vm_id] = vm
        return vm

    def snapshot_vm(self, vm: MicroVM, snapshot_name: str = None) -> str:
        """Snapshots a running VM."""
        if not snapshot_name:
            snapshot_name = f"{vm.vm_id}_{int(os.path.getmtime(vm.socket_path))}" # timestampish
            
        snap_dir = self.snapshots_dir / snapshot_name
        snap_dir.mkdir(exist_ok=True)
        
        snapshot_path = snap_dir / "snapshot_file"
        mem_path = snap_dir / "mem_file"
        
        vm.pause()
        vm.snapshot(str(snapshot_path), str(mem_path))
        vm.resume() 
        
        # Save snapshot metadata including VM configuration
        import json
        import shutil
        
        # Copy rootfs to snapshot directory
        vm_meta = self._get_metadata(vm.vm_id)
        source_rootfs = Path(vm_meta.get("rootfs_path"))
        snap_rootfs = snap_dir / "rootfs.ext4"
        if source_rootfs.exists():
            shutil.copy2(source_rootfs, snap_rootfs)
        
        snapshot_meta = {
            "snapshot_name": snapshot_name,
            "source_vm_id": vm.vm_id,
            "vcpu": vm_meta.get("vcpu", 1),
            "mem_mib": vm_meta.get("mem_mib", 128),
            "image": vm_meta.get("image", "unknown"),
            "rootfs_path": str(snap_rootfs), # Point to the snapshot copy
            "network_config": vm_meta.get("network_config"),
            "created_at": os.path.getmtime(str(snapshot_path)) if os.path.exists(str(snapshot_path)) else None
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

    def list_vms(self):
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
                    vms.append(meta)
                else:
                    # Socket missing, assume stopped
                    if meta.get("status") != "stopped":
                        meta["status"] = "stopped"
                        # Optional: Update metadata file to reflect reality?
                        # self._save_metadata(vm_id, meta)
                    vms.append(meta)
            except Exception:
                pass
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
                        logger.warning(f"Could not decode metadata for snapshot {snap_dir.name}")
                        snapshots.append({
                            "id": snap_dir.name,
                            "path": str(snap_dir),
                            "status": "metadata_corrupted"
                        })
                else:
                    snapshots.append({
                        "id": snap_dir.name,
                        "path": str(snap_dir),
                        "status": "no_metadata"
                    })
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
            
        return vm

class ManagedMicroVM(MicroVM):
    def __init__(self, vm_id: str, socket_path: str, bandsox: 'BandSox'):
        super().__init__(vm_id, socket_path)
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
             if '"status": "ready"' in line or '"status": "ready"' in line.replace(" ", ""):
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
                 logger.warning(f"Attempted to pause non-existent/deleted VM: {self.vm_id}")
                 raise e
            raise e

    def resume(self):
        try:
            super().resume()
            self.bandsox.update_vm_status(self.vm_id, "running")
        except Exception as e:
            if "Connection refused" in str(e) or isinstance(e, FileNotFoundError):
                 logger.warning(f"Attempted to resume non-existent/deleted VM: {self.vm_id}")
                 raise e
            raise e

    def stop(self):
        # Try to kill by PID if available
        meta = self.bandsox._get_metadata(self.vm_id)
        pid = meta.get("pid")
        
        if pid:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
                # Wait a bit? We can't waitpid on non-child easily without loop
                # Just send kill if it doesn't die
                time.sleep(0.5)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass # Already dead
            except PermissionError:
                logger.error(f"Permission denied killing PID {pid}")

        super().stop()
        self.bandsox.update_vm_status(self.vm_id, "stopped")
        
        # Also clear agent_ready in metadata
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
                     pass # connection might fail if socket not ready yet
            
            # 2. Check if process died (if we own it)
            if self.process and self.process.poll() is not None:
                raise Exception(f"VM process exited unexpectedly with code {self.process.returncode}")

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

    def delete(self):
        self.bandsox.delete_vm(self.vm_id)



