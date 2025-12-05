import os
import uuid
import logging
from pathlib import Path
from .vm import MicroVM, DEFAULT_KERNEL_PATH
from .image import build_rootfs

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


    def create_vm(self, docker_image: str, name: str = None, vcpu: int = 1, mem_mib: int = 128, kernel_path: str = DEFAULT_KERNEL_PATH, enable_networking: bool = True) -> MicroVM:
        """Creates and starts a new VM from a Docker image."""
        vm_id = str(uuid.uuid4())
        logger.info(f"Creating VM {vm_id} from {docker_image}")
        
        # 1. Build Rootfs
        sanitized_name = docker_image.replace(":", "_").replace("/", "_")
        base_rootfs = self.images_dir / f"{sanitized_name}.ext4"
        
        if not base_rootfs.exists():
            build_rootfs(docker_image, str(base_rootfs))
            
        # Copy to instance specific path
        instance_rootfs = self.images_dir / f"{vm_id}.ext4"
        import shutil
        shutil.copy2(base_rootfs, instance_rootfs)
        
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
            "created_at": time.time(),
            "status": "running"
        })
        
        self.active_vms[vm_id] = vm
        return vm

    def create_vm_from_dockerfile(self, dockerfile_path: str, tag: str = None, name: str = None, **kwargs) -> MicroVM:
        """Creates a VM from a Dockerfile."""
        if not tag:
            tag = f"bandsox-build-{uuid.uuid4()}"
            
        from .image import build_image_from_dockerfile
        build_image_from_dockerfile(dockerfile_path, tag)
        
        return self.create_vm(tag, name=name, **kwargs)

    def restore_vm(self, snapshot_id: str, enable_networking: bool = True) -> MicroVM:
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
        vm.load_snapshot(str(snapshot_path), str(mem_path), enable_networking=enable_networking)
        
        vm.resume()
        # Agent is already running in the restored VM
        vm.agent_ready = True
        
        # Save metadata (inherit from snapshot if possible, or create new)
        self._save_metadata(new_vm_id, {
            "id": new_vm_id,
            "name": f"from-{snapshot_id}",  # Descriptive name for restored VMs
            "image": snapshot_meta.get("image", "snapshot:" + snapshot_id),
            "vcpu": snapshot_meta.get("vcpu", 1),
            "mem_mib": snapshot_meta.get("mem_mib", 128),
            "created_at": time.time(),
            "status": "running",
            "restored_from": snapshot_id,
            "rootfs_path": str(self.images_dir / f"{new_vm_id}.ext4")
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
        vm_meta = self._get_metadata(vm.vm_id)
        snapshot_meta = {
            "snapshot_name": snapshot_name,
            "source_vm_id": vm.vm_id,
            "vcpu": vm_meta.get("vcpu", 1),
            "mem_mib": vm_meta.get("mem_mib", 128),
            "image": vm_meta.get("image", "unknown"),
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
        return ManagedMicroVM(vm_id, str(socket_path), self)

class ManagedMicroVM(MicroVM):
    def __init__(self, vm_id: str, socket_path: str, bandsox: 'BandSox'):
        super().__init__(vm_id, socket_path)
        self.bandsox = bandsox

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
        super().stop()
        self.bandsox.update_vm_status(self.vm_id, "stopped")

    def delete(self):
        self.bandsox.delete_vm(self.vm_id)

