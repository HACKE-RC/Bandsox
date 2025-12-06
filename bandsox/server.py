from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os
from .core import BandSox
import logging
import asyncio
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bandsox-server")

app = FastAPI()
bs = BandSox(storage_dir=os.getcwd() + "/storage")

# Serve static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def read_index():
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/vm_details")
async def read_vm_details():
    return FileResponse(os.path.join(static_dir, "vm_details.html"))

@app.get("/terminal")
async def read_terminal():
    return FileResponse(os.path.join(static_dir, "terminal.html"))

@app.get("/api/vms")
def list_vms():
    return bs.list_vms()

@app.get("/api/snapshots")
def list_snapshots():
    return bs.list_snapshots()

class CreateVMRequest(BaseModel):
    image: str
    name: str = None
    vcpu: int = 1
    mem_mib: int = 128
    enable_networking: bool = True

@app.post("/api/vms")
def create_vm(req: CreateVMRequest):
    logger.info(f"Received create request for {req.image}")
    try:
        vm = bs.create_vm(req.image, name=req.name, vcpu=req.vcpu, mem_mib=req.mem_mib, enable_networking=req.enable_networking)
        return {"id": vm.vm_id, "status": "created"}
    except Exception as e:
        logger.error(f"Failed to create VM: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: str):
    logger.info(f"Received delete request for snapshot {snapshot_id}")
    try:
        bs.delete_snapshot(snapshot_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "deleted"}

@app.post("/api/vms/{vm_id}/stop")
def stop_vm(vm_id: str):
    logger.info(f"Received stop request for VM {vm_id}")
    vm = bs.get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    try:
        vm.stop()
    except Exception as e:
        if "Connection refused" in str(e):
            bs.update_vm_status(vm_id, "stopped")
            return {"status": "stopped"}
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "stopped"}

@app.post("/api/vms/{vm_id}/pause")
def pause_vm(vm_id: str):
    vm = bs.get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    try:
        vm.pause()
    except Exception as e:
        if "Connection refused" in str(e):
            bs.update_vm_status(vm_id, "stopped")
            raise HTTPException(status_code=409, detail="VM is not running")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "paused"}

@app.post("/api/vms/{vm_id}/resume")
def resume_vm(vm_id: str):
    vm = bs.get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    try:
        vm.resume()
    except Exception as e:
        if "Connection refused" in str(e):
            bs.update_vm_status(vm_id, "stopped")
            raise HTTPException(status_code=409, detail="VM is not running")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "resumed"}

class SnapshotRequest(BaseModel):
    name: str

@app.delete("/api/vms/{vm_id}")
def delete_vm(vm_id: str):
    logger.info(f"Received delete request for VM {vm_id}")
    bs.delete_vm(vm_id)
    return {"status": "deleted"}

@app.post("/api/vms/{vm_id}/snapshot")
def snapshot_vm(vm_id: str, req: SnapshotRequest):
    vm = bs.get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    snap_id = bs.snapshot_vm(vm, req.name)
    return {"snapshot_id": snap_id}

@app.get("/api/vms/{vm_id}")
def get_vm_details(vm_id: str):
    """Get detailed information about a specific VM."""
    vm_info = bs.get_vm_info(vm_id)
    if not vm_info:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm_info

@app.get("/api/vms/{vm_id}/files")
def list_directory(vm_id: str, path: str = "/"):
    """List files in a directory inside the VM."""
    # Get VM metadata first
    vm_info = bs.get_vm_info(vm_id)
    if not vm_info:
        raise HTTPException(status_code=404, detail="VM not found")
    
    # Try to get running VM first
    vm = bs.get_vm(vm_id)
    
    # If VM is not running, create a temporary instance just for file access
    if not vm:
        # Create a temporary MicroVM instance with just the rootfs_path
        # This allows us to use debugfs even when VM is stopped
        from .vm import MicroVM
        vm = MicroVM(vm_id, "")  # Socket path not needed for debugfs
        vm.rootfs_path = vm_info.get("rootfs_path")
        vm.rootfs_path = vm_info.get("rootfs_path")
        if not vm.rootfs_path:
            # Fallback for older VMs or restored VMs without rootfs_path in metadata
            vm.rootfs_path = str(bs.images_dir / f"{vm_id}.ext4")
            logger.warning(f"rootfs_path missing in metadata for {vm_id}, using default: {vm.rootfs_path}")
    
    try:
        files = vm.list_dir(path)
        file_info_list = []
        
        for file_name in files:
            file_path = f"{path.rstrip('/')}/{file_name}" if path != "/" else f"/{file_name}"
            try:
                info = vm.get_file_info(file_path)
                file_info_list.append({
                    "name": file_name,
                    "path": file_path,
                    "size": info.get("size", 0),
                    "is_dir": info.get("is_dir", False),
                    "is_file": info.get("is_file", False),
                    "mtime": info.get("mtime", 0)
                })
            except Exception as e:
                # If we can't get info, just add basic data
                file_info_list.append({
                    "name": file_name,
                    "path": file_path,
                    "size": 0,
                    "is_dir": False,
                    "is_file": True,
                    "mtime": 0
                })
        
        return {"path": path, "files": file_info_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list directory: {str(e)}")

@app.get("/api/vms/{vm_id}/download")
def download_file(vm_id: str, path: str):
    """Download a file from the VM."""
    from fastapi.responses import StreamingResponse
    import tempfile
    
    # Get VM metadata first
    vm_info = bs.get_vm_info(vm_id)
    if not vm_info:
        raise HTTPException(status_code=404, detail="VM not found")
    
    # Try to get running VM first
    vm = bs.get_vm(vm_id)
    
    # If VM is not running, create a temporary instance just for file access
    if not vm:
        from .vm import MicroVM
        vm = MicroVM(vm_id, "")
        vm.rootfs_path = vm_info.get("rootfs_path")
        vm.rootfs_path = vm_info.get("rootfs_path")
        if not vm.rootfs_path:
            # Fallback for older VMs or restored VMs without rootfs_path in metadata
            vm.rootfs_path = str(bs.images_dir / f"{vm_id}.ext4")
            logger.warning(f"rootfs_path missing in metadata for {vm_id}, using default: {vm.rootfs_path}")
    
    try:
        # Create a temporary file to store the downloaded content
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        temp_path = temp_file.name
        temp_file.close()
        
        # Download the file from VM to temp location
        vm.download_file(path, temp_path)
        
        # Get the filename from the path
        filename = os.path.basename(path)
        
        # Stream the file
        def iterfile():
            with open(temp_path, 'rb') as f:
                yield from f
            # Clean up temp file after streaming
            os.unlink(temp_path)
        
        return StreamingResponse(
            iterfile(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")

@app.websocket("/api/vms/{vm_id}/terminal")
async def terminal_endpoint(websocket: WebSocket, vm_id: str, cols: int = 80, rows: int = 24):
    await websocket.accept()
    
    vm = bs.get_vm(vm_id)
    if not vm:
        await websocket.close(code=4004, reason="VM not found or not running")
        return

    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    
    def on_stdout(data):
        # data is base64 encoded string from agent
        asyncio.run_coroutine_threadsafe(queue.put(data), loop)

    def on_exit(code):
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    try:
        # Run blocking start_pty_session in a thread to avoid blocking the event loop
        # and causing WebSocket timeout (1006)
        session_id = await asyncio.to_thread(vm.start_pty_session, "/bin/sh", cols, rows, on_stdout=on_stdout, on_exit=on_exit)
    except Exception as e:
        logger.error(f"Failed to start PTY session: {e}")
        await websocket.close(code=4000, reason=f"Failed to start session: {str(e)}")
        return

    async def sender():
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                await websocket.send_text(data)
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    sender_task = asyncio.create_task(sender())

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg["type"] == "input":
                    # Input is base64 encoded from client
                    vm.send_session_input(session_id, msg["data"], encoding="base64")
                elif msg["type"] == "resize":
                    vm.resize_session(session_id, msg["cols"], msg["rows"])
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        vm.kill_session(session_id)
        sender_task.cancel()

