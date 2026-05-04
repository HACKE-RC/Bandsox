from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
import os
from pathlib import Path
from .core import BandSox
from .auth import (
    load_auth_config, init_auth_config, auth_enabled,
    create_api_key, list_api_keys, revoke_api_key,
    verify_password, verify_api_key, create_session,
    validate_session, check_rate_limit, authenticate_websocket,
    get_auth_dependency, SESSION_COOKIE_NAME,
)
import logging
import asyncio
import json
import base64
import tempfile
from typing import List

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bandsox-server")

app = FastAPI()
storage_path = os.environ.get("BANDSOX_STORAGE", os.getcwd() + "/storage")
logger.info(f"Initializing BandSox with storage path: {storage_path}")
bs = BandSox(storage_dir=storage_path)

# Auth initialization
_auth_storage = Path(storage_path)
if auth_enabled(_auth_storage):
    logger.info("Authentication enabled (auth.json found)")
else:
    logger.info("Authentication disabled. Run 'bandsox auth init' to enable.")

require_auth = get_auth_dependency(_auth_storage)


# Serve static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

def _has_valid_session(request: Request) -> bool:
    if not auth_enabled(_auth_storage):
        return True
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token and validate_session(session_token, _auth_storage):
        return True
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        config = load_auth_config(_auth_storage)
        if config and verify_api_key(config, auth_header[7:]) is not None:
            return True
    return False


@app.get("/login")
async def login_page():
    return FileResponse(os.path.join(static_dir, "login.html"))


@app.get("/")
async def read_index(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/vm_details")
async def read_vm_details(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(os.path.join(static_dir, "vm_details.html"))

@app.get("/terminal")
async def read_terminal(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(os.path.join(static_dir, "terminal.html"))

@app.get("/markdown_viewer")
async def read_markdown_viewer(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(os.path.join(static_dir, "markdown_viewer.html"))

class LoginRequest(BaseModel):
    password: str

class CreateKeyRequest(BaseModel):
    name: str


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response):
    if not auth_enabled(_auth_storage):
        raise HTTPException(status_code=404, detail="Auth not enabled. Run 'bandsox auth init' first.")
    client_ip = request.client.host
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    config = load_auth_config(_auth_storage)
    if not config or not verify_password(config, req.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = create_session(_auth_storage)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
        path="/",
    )
    return {"status": "ok", "token": token}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


@app.get("/api/auth/check")
async def check_auth(request: Request):
    return {"authenticated": _has_valid_session(request)}


@app.get("/api/auth/keys", dependencies=[Depends(require_auth)])
async def get_api_keys():
    return list_api_keys(_auth_storage)


@app.post("/api/auth/keys", dependencies=[Depends(require_auth)])
async def create_key(req: CreateKeyRequest):
    key_id, plaintext = create_api_key(_auth_storage, req.name)
    return {"key_id": key_id, "key": plaintext, "name": req.name}


@app.delete("/api/auth/keys/{key_id}", dependencies=[Depends(require_auth)])
async def revoke_key(key_id: str):
    if revoke_api_key(_auth_storage, key_id):
        return {"status": "revoked"}
    raise HTTPException(status_code=404, detail="Key not found")


@app.get("/api/vms", dependencies=[Depends(require_auth)])
def list_vms(limit: int = None, metadata_equals: str = None):
    meta_filter = None
    if metadata_equals:
        try:
            meta_filter = json.loads(metadata_equals)
        except json.JSONDecodeError:
             # Or raise HTTPException?
             pass
    return bs.list_vms(limit=limit, metadata_equals=meta_filter)

@app.get("/api/projects", dependencies=[Depends(require_auth)])
def list_projects(limit: int = None, metadata_equals: str = None):
    """
    Alias for listing VMs used by the UI.
    """
    meta_filter = None
    if metadata_equals:
        try:
            meta_filter = json.loads(metadata_equals)
        except json.JSONDecodeError:
             pass
    return bs.list_vms(limit=limit, metadata_equals=meta_filter)

@app.get("/api/snapshots", dependencies=[Depends(require_auth)])
def list_snapshots():
    return bs.list_snapshots()

class CreateVMRequest(BaseModel):
    image: str
    name: str = None
    vcpu: int = 1
    mem_mib: int = 128
    enable_networking: bool = True
    force_rebuild: bool = False
    disk_size_mib: int = 4096
    env_vars: dict = None
    metadata: dict = None

@app.post("/api/vms", dependencies=[Depends(require_auth)])
def create_vm(req: CreateVMRequest):
    logger.info(f"Received create request for {req.image}")
    try:
        vm = bs.create_vm(req.image, name=req.name, vcpu=req.vcpu, mem_mib=req.mem_mib, enable_networking=req.enable_networking, force_rebuild=req.force_rebuild, disk_size_mib=req.disk_size_mib, env_vars=req.env_vars, metadata=req.metadata)
        return {"id": vm.vm_id, "status": "created"}
    except Exception as e:
        logger.error(f"Failed to create VM: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/from-dockerfile", dependencies=[Depends(require_auth)])
async def create_vm_from_dockerfile(
    dockerfile: UploadFile = File(...),
    tag: str = Form(None),
    name: str = Form(None),
    vcpu: int = Form(1),
    mem_mib: int = Form(128),
    disk_size_mib: int = Form(4096),
    force_rebuild: bool = Form(False),
    env_vars: str = Form(None),
    metadata: str = Form(None),
):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".Dockerfile") as tmp:
            tmp.write(await dockerfile.read())
            dockerfile_path = tmp.name
        try:
            vm = bs.create_vm_from_dockerfile(
                dockerfile_path,
                tag=tag,
                name=name,
                vcpu=vcpu,
                mem_mib=mem_mib,
                disk_size_mib=disk_size_mib,
                force_rebuild=force_rebuild,
                env_vars=json.loads(env_vars) if env_vars else None,
                metadata=json.loads(metadata) if metadata else None,
            )
        finally:
            os.unlink(dockerfile_path)
        return {"id": vm.vm_id, "status": "created"}
    except Exception as e:
        logger.error(f"Failed to create VM from Dockerfile: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class RestoreVMRequest(BaseModel):
    name: str = None
    enable_networking: bool = True
    env_vars: dict = None
    metadata: dict = None

@app.post("/api/snapshots/{snapshot_id}/restore", dependencies=[Depends(require_auth)])
def restore_snapshot(snapshot_id: str, req: RestoreVMRequest):
    logger.info(f"Received restore request for snapshot {snapshot_id}")
    try:
        vm = bs.restore_vm(snapshot_id, name=req.name, enable_networking=req.enable_networking, env_vars=req.env_vars, metadata=req.metadata)
        return {"id": vm.vm_id, "status": "restored"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    except Exception as e:
        logger.error(f"Failed to restore VM: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/snapshots/{snapshot_id}", dependencies=[Depends(require_auth)])
def delete_snapshot(snapshot_id: str):
    logger.info(f"Received delete request for snapshot {snapshot_id}")
    try:
        bs.delete_snapshot(snapshot_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "deleted"}

class UpdateSnapshotMetadataRequest(BaseModel):
    metadata: dict

@app.put("/api/snapshots/{snapshot_id}/metadata", dependencies=[Depends(require_auth)])
def update_snapshot_metadata(snapshot_id: str, req: UpdateSnapshotMetadataRequest):
    logger.info(f"Received metadata update request for snapshot {snapshot_id}")
    try:
        updated_meta = bs.update_snapshot_metadata(snapshot_id, req.metadata)
        return updated_meta
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    except Exception as e:
        logger.error(f"Failed to update metadata for snapshot {snapshot_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class RenameSnapshotRequest(BaseModel):
    name: str

@app.put("/api/snapshots/{snapshot_id}/name", dependencies=[Depends(require_auth)])
def rename_snapshot(snapshot_id: str, req: RenameSnapshotRequest):
    """Rename a snapshot."""
    try:
        bs.rename_snapshot(snapshot_id, req.name)
        return {"status": "renamed", "name": req.name}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    except Exception as e:
        logger.error(f"Failed to rename snapshot {snapshot_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vm_id}/stop", dependencies=[Depends(require_auth)])
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

@app.post("/api/vms/{vm_id}/pause", dependencies=[Depends(require_auth)])
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

@app.post("/api/vms/{vm_id}/resume", dependencies=[Depends(require_auth)])
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
    metadata: dict = None

@app.delete("/api/vms/{vm_id}", dependencies=[Depends(require_auth)])
def delete_vm(vm_id: str):
    logger.info(f"Received delete request for VM {vm_id}")
    bs.delete_vm(vm_id)
    return {"status": "deleted"}

@app.post("/api/vms/{vm_id}/snapshot", dependencies=[Depends(require_auth)])
def snapshot_vm(vm_id: str, req: SnapshotRequest):
    vm = bs.get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    snap_id = bs.snapshot_vm(vm, req.name, metadata=req.metadata)
    return {"snapshot_id": snap_id}

@app.get("/api/vms/{vm_id}", dependencies=[Depends(require_auth)])
def get_vm_details(vm_id: str):
    """Get detailed information about a specific VM."""
    vm_info = bs.get_vm_info(vm_id)
    if not vm_info:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm_info

class UpdateMetadataRequest(BaseModel):
    metadata: dict

class RenameRequest(BaseModel):
    name: str

class ExecRequest(BaseModel):
    command: str
    timeout: int = 30

class ExecPythonRequest(BaseModel):
    code: str
    cwd: str = "/tmp"
    packages: List[str] = None
    timeout: int = 60
    cleanup_venv: bool = True

class WriteFileRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"
    append: bool = False

class HttpProxyRequest(BaseModel):
    port: int
    path: str = "/"
    method: str = "GET"
    headers: dict = None
    body: str = None
    json_body: dict = None
    timeout: int = 30

@app.put("/api/vms/{vm_id}/metadata", dependencies=[Depends(require_auth)])
def update_vm_metadata(vm_id: str, req: UpdateMetadataRequest):
    """Update the metadata of a VM."""
    try:
        updated_meta = bs.update_vm_metadata(vm_id, req.metadata)
        return updated_meta
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="VM not found")
    except Exception as e:
        logger.error(f"Failed to update metadata for VM {vm_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/vms/{vm_id}/name", dependencies=[Depends(require_auth)])
def rename_vm(vm_id: str, req: RenameRequest):
    """Rename a VM."""
    try:
        bs.rename_vm(vm_id, req.name)
        return {"status": "renamed", "name": req.name}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="VM not found")
    except Exception as e:
        logger.error(f"Failed to rename VM {vm_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _get_running_vm_or_404(vm_id: str):
    vm = bs.get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found or not running")
    return vm


def _get_vm_for_file_access_or_404(vm_id: str):
    """Return a VM object suitable for file access, even if not running.

    For stopped or unreachable guests we synthesize a lightweight MicroVM
    wrapper carrying only ``rootfs_path`` so file reads can fall back to
    debugfs against the ext4 image on disk.
    """
    vm_info = bs.get_vm_info(vm_id)
    if not vm_info:
        raise HTTPException(status_code=404, detail="VM not found")

    vm = bs.get_vm(vm_id)
    if vm:
        return vm

    from .vm import MicroVM

    vm = MicroVM(vm_id, "")
    vm.rootfs_path = vm_info.get("rootfs_path")
    if not vm.rootfs_path:
        vm.rootfs_path = str(bs.images_dir / f"{vm_id}.ext4")
        logger.warning(
            f"rootfs_path missing in metadata for {vm_id}, using default: {vm.rootfs_path}"
        )
    return vm

@app.post("/api/vms/{vm_id}/exec", dependencies=[Depends(require_auth)])
def exec_command(vm_id: str, req: ExecRequest):
    """Run a blocking shell command inside a VM and capture stdout/stderr."""
    vm = _get_running_vm_or_404(vm_id)
    stdout = []
    stderr = []
    try:
        exit_code = vm.exec_command(
            req.command,
            on_stdout=lambda line: stdout.append(str(line)),
            on_stderr=lambda line: stderr.append(str(line)),
            timeout=req.timeout,
        )
        return {
            "exit_code": exit_code,
            "stdout": "".join(stdout),
            "stderr": "".join(stderr),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vm_id}/exec-python", dependencies=[Depends(require_auth)])
def exec_python(vm_id: str, req: ExecPythonRequest):
    """Run Python inside a VM and return captured output."""
    vm = _get_running_vm_or_404(vm_id)
    try:
        return vm.exec_python_capture(
            req.code,
            cwd=req.cwd,
            packages=req.packages,
            timeout=req.timeout,
            cleanup_venv=req.cleanup_venv,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/vms/{vm_id}/read-file", dependencies=[Depends(require_auth)])
def read_file(vm_id: str, path: str):
    """Read a UTF-8 file from inside a VM."""
    vm = _get_vm_for_file_access_or_404(vm_id)
    try:
        return {"path": path, "content": vm.get_file_contents(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vm_id}/write-file", dependencies=[Depends(require_auth)])
def write_file(vm_id: str, req: WriteFileRequest):
    """Write string content to a file inside a VM."""
    vm = _get_running_vm_or_404(vm_id)
    try:
        raw = (
            base64.b64decode(req.content)
            if req.encoding == "base64"
            else req.content.encode("utf-8")
        )
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            vm.upload_file(tmp_path, req.path, append=req.append)
        finally:
            os.unlink(tmp_path)
        return {"status": "appended" if req.append else "written", "path": req.path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vm_id}/append-file", dependencies=[Depends(require_auth)])
def append_file(vm_id: str, req: WriteFileRequest):
    """Append string content to a file inside a VM."""
    vm = _get_running_vm_or_404(vm_id)
    try:
        raw = (
            base64.b64decode(req.content)
            if req.encoding == "base64"
            else req.content.encode("utf-8")
        )
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            vm.upload_file(tmp_path, req.path, append=True)
        finally:
            os.unlink(tmp_path)
        return {"status": "appended", "path": req.path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/vms/{vm_id}/file-info", dependencies=[Depends(require_auth)])
def file_info(vm_id: str, path: str):
    """Get file metadata from inside a VM."""
    vm = _get_running_vm_or_404(vm_id)
    try:
        return {"path": path, "info": vm.get_file_info(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vm_id}/upload", dependencies=[Depends(require_auth)])
async def upload_file(
    vm_id: str,
    remote_path: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload multipart file content into a VM."""
    vm = _get_running_vm_or_404(vm_id)
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        try:
            vm.upload_file(tmp_path, remote_path)
        finally:
            os.unlink(tmp_path)
        return {"status": "uploaded", "path": remote_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vms/{vm_id}/http", dependencies=[Depends(require_auth)])
def proxy_http(vm_id: str, req: HttpProxyRequest):
    """Proxy an HTTP request to a service running inside the VM."""
    vm = _get_running_vm_or_404(vm_id)
    try:
        resp = vm.send_http_request(
            port=req.port,
            path=req.path,
            method=req.method,
            headers=req.headers,
            data=req.body,
            json=req.json_body,
            timeout=req.timeout,
        )
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.text,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/vms/{vm_id}/files", dependencies=[Depends(require_auth)])
def list_directory(vm_id: str, path: str = "/"):
    """List files in a directory inside the VM."""
    vm = _get_vm_for_file_access_or_404(vm_id)
    
    try:
        files = vm.list_dir(path)
        logger.info(f"Listed files for VM {vm_id} at {path}: {files}")
        file_info_list = []
        
        for entry in files:
            # list_dir (agent) returns dicts with {name, type, size, mtime}
            if isinstance(entry, dict):
                name = entry.get("name")
                file_path = f"{path.rstrip('/')}/{name}" if path != "/" else f"/{name}"
                file_info_list.append({
                    "name": name,
                    "path": file_path,
                    "size": entry.get("size", 0),
                    "is_dir": entry.get("type") == "directory",
                    "is_file": entry.get("type") == "file",
                    "mtime": entry.get("mtime", 0)
                })
            else:
                # Handle string case if fallback implementation returns list of names
                file_name = str(entry)
                file_path = f"{path.rstrip('/')}/{file_name}" if path != "/" else f"/{file_name}"
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
        logger.error(f"Failed to list directory: {e}")
        # If agent is not ready/vm stopped and we don't support it yet
        raise HTTPException(status_code=500, detail=f"Failed to list directory. VM must be running. Error: {str(e)}")

@app.get("/api/vms/{vm_id}/download", dependencies=[Depends(require_auth)])
def download_file(vm_id: str, path: str):
    """Download a file from the VM."""
    from fastapi.responses import StreamingResponse

    vm = _get_vm_for_file_access_or_404(vm_id)
    
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
    if not await authenticate_websocket(websocket, _auth_storage):
        await websocket.accept()
        await websocket.close(code=4001, reason="Unauthorized")
        return

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
