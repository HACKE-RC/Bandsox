# BandSox documentation

Python library for managing Firecracker microVMs. Create, manage, and snapshot sandboxes from Docker images.

## Table of contents

- [Quick start](#quick-start)
- [Core concepts](#core-concepts)
- [Usage guide](#usage-guide)
  - [Initialization](#initialization)
  - [Creating VMs](#creating-vms)
  - [Executing commands](#executing-commands)
  - [File operations](#file-operations)
  - [Snapshots](#snapshots)
- [Authentication](#authentication)
- [CLI reference](#cli-reference)
- [HTTP API](#http-api)
- [Class reference](#class-reference)
- [Caveats and troubleshooting](#caveats-and-troubleshooting)

## Quick start

```python
from bandsox.core import BandSox

# 1. Initialize the manager
#    (Default storage at /var/lib/bandsox, requires write permissions)
manager = BandSox()

# 2. Create a VM from a Docker image
#    (Requires internet to pull the image if not cached)
vm = manager.create_vm("alpine:latest", vcpu=2, mem_mib=1024)

print(f"VM Created with ID: {vm.vm_id}")

# 3. Run a command inside the VM
result = vm.exec_command("echo Hello from Firecracker!")
print(f"Command exit code: {result}")

# 4. Cleanup
vm.stop()
vm.delete()
```

## Core concepts

- `BandSox` -- the main controller. Manages storage, networking, and VM lifecycle.
- `MicroVM` -- a single running Firecracker instance. Talks to the guest agent and the VMM.
- `Agent` -- a small process inside the guest OS that runs commands and handles file ops on behalf of the host.
- `Rootfs` -- the VM filesystem, built from a Docker image.

## Usage guide

### Initialization

Start with the `BandSox` class.

```python
from bandsox.core import BandSox

manager = BandSox(storage_dir="/path/to/storage")
```

The storage directory holds images, sockets, and metadata -- set permissions accordingly. Large artifacts (kernel, CNI plugins, rootfs images) are not in git; run `bandsox init` to download them.

To use a running BandSox server instead of managing Firecracker locally, pass `server_url` and include auth headers:

```python
from bandsox.core import BandSox

manager = BandSox(
    server_url="http://localhost:8000",
    headers={"Authorization": "Bearer bsx_your_key_here"},
)
vm = manager.create_vm("python:3.11-slim", enable_networking=False)

result = vm.exec_python_capture("print('hello from the remote server')")
print(result["stdout"])

vm.stop()
```

You can also pass the URL as the first argument: `BandSox("http://localhost:8000")`.

### Creating VMs

From a Docker image:

```python
vm = manager.create_vm(
    docker_image="python:3.9-slim",
    name="my-python-sandbox",
    vcpu=2,
    mem_mib=512,
    enable_networking=True
)
```

From a Dockerfile:

```python
vm = manager.create_vm_from_dockerfile(
    dockerfile_path="./Dockerfile",
    tag="custom-image:v1",
    name="my-custom-vm"
)
```

### Executing commands

Several ways to run commands:

**1. Blocking (`exec_command`)**
Waits for the command to finish.

```python
code = vm.exec_command("ls -la /", on_stdout=lambda line: print(f"OUT: {line}"), timeout=10)
```

**2. Background (`start_session`)**
Returns a session ID for long-running processes.

```python
session_id = vm.start_session("sleep 100")
# ... do other things ...
vm.kill_session(session_id)
```

**3. Interactive PTY (`start_pty_session`)**
Allocates a pseudo-terminal. Use when the program expects a TTY (shells, interactive CLIs).

```python
session_id = vm.start_pty_session("/bin/sh", cols=80, rows=24)
vm.send_session_input(session_id, "echo Interactive\n")
```

**4. Python execution (`exec_python`)**
Run Python code with isolated dependencies (uses `uv` for package installation).

```python
# Simple
vm.exec_python("print('Hello from Python!')")

# With dependencies
vm.exec_python(
    code="import requests; print(requests.get('https://example.com').status_code)",
    packages=["requests"]
)
```

**5. Python execution with capture (`exec_python_capture`)**
Captures output and returns a result dict. Does not raise on errors.

```python
result = vm.exec_python_capture("print('hello')")
if result['success']:
    print(f"Output: {result['output']}")
else:
    print(f"Error: {result['error']}")
```

### File operations

File transfers go through the guest agent.

```python
# Upload a file (timeout scales with file size: 60s minimum + 30s per MB)
vm.upload_file("./local_script.py", "/app/script.py")

# Download a file
vm.download_file("/app/result.txt", "./result.txt")

# Read file contents as a string
content = vm.get_file_contents("/etc/hostname")
```

### Snapshots

Save the memory and disk state of a running VM, then restore it later.

```python
# Create a snapshot (VM pauses briefly)
snapshot_id = manager.snapshot_vm(vm, snapshot_name="checkpoint-1")

# Restore into a new VM
restored_vm = manager.restore_vm(snapshot_id)
```

## Authentication

Auth is off by default. All endpoints are open until you run `bandsox auth init`, which creates `auth.json` in the storage directory. Delete that file to disable auth again.

### Enabling auth

```bash
sudo bandsox auth init --storage /var/lib/sandbox
```

This generates an admin password and an initial API key, and prints both to stdout. The API key is only shown once. The CLI will offer to save it to `~/.bandsox/credentials`.

### How it works

When `auth.json` exists, the server supports two methods:

**API keys** for CLI, SDK, and direct HTTP calls. Pass them as a Bearer token:

```
Authorization: Bearer bsx_your_key_here
```

Keys are stored as SHA-256 hashes in `auth.json`. The plaintext is only shown at creation time.

**Session cookies** for the web dashboard. Log in at `/login` with the admin password to get a `bandsox_session` cookie. Sessions last 24 hours. They're HMAC-signed tokens (expiry + SHA-256 signature), so they survive server restarts.

WebSocket terminal passes auth via a `token` query parameter since the browser WebSocket API can't send custom headers. The dashboard handles this automatically after login.

### Auth endpoints

These endpoints work whether auth is enabled or not:

- `POST /api/auth/login` -- log in with admin password, get a session cookie. Rate-limited to 10 attempts per minute per IP. Returns 404 if auth isn't enabled.
  - Body: `{ "password": "..." }`
  - Returns: `{ "status": "ok", "token": "..." }`
- `POST /api/auth/logout` -- clear cookie.
- `GET /api/auth/check` -- returns `{ "authenticated": true/false }`. Always returns true when auth is disabled.

These endpoints require authentication (when auth is enabled):

- `GET /api/auth/keys` -- list all API keys (IDs and names, no secrets).
- `POST /api/auth/keys` -- create a new API key. Returns the plaintext key once.
  - Body: `{ "name": "my-key" }`
  - Returns: `{ "key_id": "bsx_k_...", "key": "bsx_...", "name": "my-key" }`
- `DELETE /api/auth/keys/{key_id}` -- revoke a key.

### CLI auth commands

```bash
# Enable auth (generates password + API key)
sudo bandsox auth init --storage /var/lib/sandbox

# Set or reset the admin password (direct file access, no server needed)
sudo bandsox auth set-password --storage /var/lib/sandbox

# Create a key via the server API (requires existing auth)
bandsox auth create-key my-key

# List and revoke keys
bandsox auth list-keys
bandsox auth revoke-key bsx_k_<id>
```

Credentials are stored at `~/.bandsox/credentials` with mode 600.

## CLI reference

The `bandsox` CLI wraps the server and API.

- `bandsox init` -- download required artifacts.
  - Flags: `--kernel-url`, `--kernel-output`, `--skip-kernel`, `--cni-url`, `--cni-dir`, `--skip-cni`, `--rootfs-url`, `--rootfs-output`, `--skip-rootfs`, `--force`
  - Downloads `vmlinux`, CNI plugins (tgz), and optionally a base `.ext4` rootfs. Skips existing files unless `--force` is set.

    ```bash
    bandsox init --rootfs-url ./bandsox-base.ext4
    ```

- `bandsox serve [--host 0.0.0.0] [--port 8000] [--storage /var/lib/sandbox]` -- run the FastAPI server. Auth is off unless `auth.json` exists.
- `bandsox create <image> [--name NAME] [--vcpu N] [--mem MiB] [--disk-size MiB] [--host HOST] [--port PORT]` -- create a VM from a Docker image via the server API.
- `bandsox vm list|stop|pause|resume|delete|save|rename ...` -- manage VMs.
- `bandsox snapshot list|delete|restore|rename ...` -- manage snapshots.
- `bandsox terminal <vm_id> [--host HOST] [--port PORT]` -- connect to a VM's terminal over WebSocket.
- `bandsox auth init|set-password|create-key|list-keys|revoke-key ...` -- manage authentication (off by default).
- `bandsox cleanup` -- remove stale TAP devices.

## HTTP API

Base URL: `http://HOST:PORT`

When auth is enabled (`auth.json` exists), all `/api/` endpoints except auth login/logout/check require a Bearer token or session cookie. When auth is disabled, all endpoints are open.

### Auth

- `POST /api/auth/login` -- log in, get session cookie.
- `POST /api/auth/logout` -- log out, clear session.
- `GET /api/auth/check` -- check if authenticated.
- `GET /api/auth/keys` -- list API keys.
- `POST /api/auth/keys` -- create an API key.
- `DELETE /api/auth/keys/{key_id}` -- revoke an API key.

### VMs

- `GET /api/vms` -- list VMs.
- `POST /api/vms` -- create a VM from an image.
  - Body: `{ "image": "alpine:latest", "name": "...", "vcpu": 1, "mem_mib": 128, "enable_networking": true, "force_rebuild": false, "disk_size_mib": 4096 }`
- `GET /api/vms/{vm_id}` -- get VM details.
- `POST /api/vms/{vm_id}/stop|pause|resume` -- lifecycle operations.
- `DELETE /api/vms/{vm_id}` -- delete a VM.
- `POST /api/vms/{vm_id}/snapshot` -- snapshot a running VM.
  - Body: `{ "name": "snap-name" }`
- `POST /api/vms/{vm_id}/exec` -- run a blocking command.
  - Body: `{ "command": "echo hello", "timeout": 30 }`
- `POST /api/vms/{vm_id}/exec-python` -- run Python and return captured output.
- `GET /api/vms/{vm_id}/files?path=/` -- list files inside the VM.
- `GET /api/vms/{vm_id}/read-file?path=/etc/hosts` -- read a UTF-8 file.
- `POST /api/vms/{vm_id}/write-file` -- write a UTF-8 or base64-encoded file.
- `GET /api/vms/{vm_id}/file-info?path=/etc/hosts` -- get file metadata.
- `POST /api/vms/{vm_id}/upload` -- upload a multipart file.
- `GET /api/vms/{vm_id}/download?path=/etc/hosts` -- download a file.
- `POST /api/vms/{vm_id}/http` -- proxy an HTTP request to a service inside the VM.
- `WS /api/vms/{vm_id}/terminal?cols=80&rows=24&token=<session_or_api_key>` -- interactive terminal (WebSocket).

### Snapshots

- `GET /api/snapshots` -- list snapshots.
- `DELETE /api/snapshots/{snapshot_id}` -- delete a snapshot.
- `POST /api/snapshots/{snapshot_id}/restore` -- restore into a new VM.
  - Body: `{ "name": "optional-name", "enable_networking": true }`

### Static pages

All pages redirect to `/login` if not authenticated.

- `GET /` -- dashboard.
- `GET /login` -- login page.
- `GET /terminal` -- web terminal page.
- `GET /vm_details` -- VM details page.
- `GET /markdown_viewer` -- markdown viewer.

## Class reference

### `BandSox`

| Method | Description |
| --- | --- |
| `create_vm(docker_image, name=None, vcpu=1, mem_mib=128, ...)` | Create a new VM. |
| `create_vm_from_dockerfile(dockerfile_path, tag, ...)` | Build an image and create a VM. |
| `restore_vm(snapshot_id, enable_networking=True)` | Restore a VM from a snapshot. |
| `snapshot_vm(vm, snapshot_name=None)` | Snapshot a running VM. |
| `delete_vm(vm_id)` | Stop and delete a VM and its resources. |
| `list_vms()` | List all known VMs. |
| `get_owner(vm_id)` | Return the `MicroVM` instance. |

### `MicroVM`

| Method | Description |
| --- | --- |
| `start()`, `stop()`, `pause()`, `resume()` | Lifecycle control. |
| `exec_command(cmd, on_stdout=None, timeout=30)` | Run a command (blocking). |
| `exec_python(code, cwd, packages, ...)` | Run Python code with isolated env using `uv`. |
| `exec_python_capture(code, packages, ...)` | Run Python and return output dict. |
| `start_session(cmd)` | Run a command (background). |
| `upload_file(local, remote, timeout=None)` | Upload a file. Timeout scales with file size (60s + 30s/MB). |
| `download_file(remote, local)` | Download a file (pauses VM). |
| `get_file_contents(remote)` | Read file content (pauses VM). |

## Caveats and troubleshooting

### 1. Root privileges and networking

Networking (`enable_networking=True`) requires sudo.

- The library runs `sudo ip ...` and `sudo iptables ...` to configure TAP devices and NAT.
- Run the script as root or have passwordless sudo for networking commands.
- If you don't have sudo access, create VMs with `enable_networking=False`.

### 2. File operations pause the VM

`upload_file` and `download_file` use `debugfs` on the ext4 filesystem, so the VM is paused during transfer to avoid corruption. Network connections may time out and real-time processes will be interrupted. For small files, `cat` via `exec_command` avoids the pause but is less reliable for binary data.

### 3. Kernel dependencies

VMs need a compatible Linux kernel binary (`vmlinux`).

- By default it looks at `/var/lib/bandsox/vmlinux`.
- Make sure this file exists, or pass `kernel_path` to `create_vm`.

### 4. Image size

The rootfs size is fixed at build time (Docker export size + overhead). If you need more space, adjust the image generation logic in `image.py`.

### 5. Snapshot compatibility

Restoring a snapshot requires the same kernel and a compatible network config.

- If you move the storage directory, move metadata and snapshots together.
- Snapshots are tied to the exact kernel binary used when they were created.

### 6. Authentication

- Auth is off by default. Enable with `bandsox auth init`. Disable by deleting `auth.json`.
- Sessions are signed tokens, so they survive server restarts. Both sessions and API keys are validated using secrets stored in `auth.json`.
- The `set-password` command works directly on the storage directory, so you can reset the password even if you're locked out of the dashboard.
- WebSocket terminal auth uses a `token` query parameter because browser WebSocket API doesn't support custom headers.
