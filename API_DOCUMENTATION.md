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
- [CLI reference](#cli-reference)
- [HTTP API](#http-api)
- [Class reference](#class-reference)
- [Caveats & troubleshooting](#caveats--troubleshooting)

## Quick start

```python
from bandsox.core import BandSox

# 1. Initialize the manager
#    (Default storage at /var/lib/bandsox, requires write permissions)
manager = BandSox()

# 2. Create a VM from a Docker image
#    (Requires internet access to pull image if not present)
vm = manager.create_vm("alpine:latest", vcpu=2, mem_mib=1024)

print(f"VM Created with ID: {vm.vm_id}")

# 3. Execute a command inside the VM
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
# Simple execution
vm.exec_python("print('Hello from Python!')")

# With isolated dependencies
vm.exec_python(
    code="import requests; print(requests.get('https://example.com').status_code)",
    packages=["requests"]
)
```

**5. Python execution with capture (`exec_python_capture`)**
Captures output and returns a result dict. Does not raise exceptions.

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

# Read file contents (as string)
content = vm.get_file_contents("/etc/hostname")
```

### Snapshots

Save the memory and disk state of a running VM, then restore it later.

```python
# Create a snapshot
# VM will be paused briefly
snapshot_id = manager.snapshot_vm(vm, snapshot_name="checkpoint-1")

# Use this snapshot ID to start new identical VMs
restored_vm = manager.restore_vm(snapshot_id)
```

## CLI reference

The `bandsox` CLI wraps the server and API.

- `bandsox init` — download required artifacts.
  - Flags: `--kernel-url`, `--kernel-output`, `--skip-kernel`, `--cni-url`, `--cni-dir`, `--skip-cni`, `--rootfs-url`, `--rootfs-output`, `--skip-rootfs`, `--force`
  - Behavior: downloads `vmlinux`, CNI plugins (tgz), and optionally a base `.ext4` rootfs. Skips existing files unless `--force` is set. Build your rootfs locally and point to a path or `file://` URL, e.g.:

    ```bash
    bandsox init --rootfs-url ./bandsox-base.ext4
    ```

  - Example CNI source (Linux/amd64): `--cni-url https://github.com/containernetworking/plugins/releases/download/v1.5.1/cni-plugins-linux-amd64-v1.5.1.tgz`
- `bandsox serve [--host 0.0.0.0] [--port 8000] [--storage /var/lib/sandbox]` — run the FastAPI dashboard/API.
- `bandsox create <image> [--name NAME] [--vcpu N] [--mem MiB] [--disk-size MiB] [--host HOST] [--port PORT]` — create a VM from a Docker image via the server API.
- `bandsox vm list|stop|pause|resume|delete|save ...` — manage VMs over the HTTP API.
- `bandsox snapshot list|delete|restore ...` — manage snapshots.
- `bandsox terminal <vm_id> [--host HOST] [--port PORT]` — connect to a VM’s terminal over WebSocket.
- `bandsox cleanup` — remove stale TAP devices.

## HTTP API

Base URL: `http://HOST:PORT`

### VMs

- `GET /api/vms` — list VMs.
- `POST /api/vms` — create a VM from an image.
  - Body: `{ "image": "alpine:latest", "name": "...", "vcpu": 1, "mem_mib": 128, "enable_networking": true, "force_rebuild": false, "disk_size_mib": 4096 }`
- `GET /api/vms/{vm_id}` — get VM details.
- `POST /api/vms/{vm_id}/stop|pause|resume` — lifecycle operations.
- `DELETE /api/vms/{vm_id}` — delete a VM.
- `POST /api/vms/{vm_id}/snapshot` — snapshot a running VM.
  - Body: `{ "name": "snap-name" }`
- `GET /api/vms/{vm_id}/files?path=/` — list files inside the VM (may start a temporary instance when stopped).
- `GET /api/vms/{vm_id}/download?path=/etc/hosts` — download a file.
- `WS /api/vms/{vm_id}/terminal?cols=80&rows=24` — interactive terminal.

### Snapshots

- `GET /api/snapshots` — list snapshots.
- `DELETE /api/snapshots/{snapshot_id}` — delete a snapshot.
- `POST /api/snapshots/{snapshot_id}/restore` — restore into a new VM.
  - Body: `{ "name": "optional-name", "enable_networking": true }`

### Static assets

- `GET /` — dashboard.
- `GET /terminal` — web terminal page.
- `GET /vm_details` — VM details page.
- `GET /markdown_viewer` — markdown viewer.

## Class reference

### `BandSox`

| Method | Description |
| --- | --- |
| `create_vm(docker_image, name=None, vcpu=1, mem_mib=128, ...)` | Creates a new VM instance. |
| `create_vm_from_dockerfile(dockerfile_path, tag, ...)` | Builds an image and creates a VM. |
| `restore_vm(snapshot_id, enable_networking=True)` | Restores a VM from a snapshot. |
| `snapshot_vm(vm, snapshot_name=None)` | create a snapshot of a running VM. |
| `delete_vm(vm_id)` | Stops and deletes a VM and its resources. |
| `list_vms()` | Lists all known VMs. |
| `get_owner(vm_id)` | Returns the `MicroVM` instance. |

### `MicroVM`

| Method | Description |
| --- | --- |
| `start()`, `stop()`, `pause()`, `resume()` | Lifecycle control. |
| `exec_command(cmd, on_stdout=None, timeout=30)` | Run a command (blocking). |
| `exec_python(code, cwd, packages, ...)` | Run Python code with isolated env using `uv`. |
| `exec_python_capture(code, packages, ...)` | Run Python and return output dict (No Except). |
| `start_session(cmd)` | Run a command (background). |
| `upload_file(local, remote, timeout=None)` | Upload a file. Timeout scales with file size (60s + 30s/MB). |
| `download_file(remote, local)` | Download a file (Pauses VM). |
| `get_file_contents(remote)` | Read file content (Pauses VM). |

## Caveats & troubleshooting

### 1. Root privileges & networking

Networking (`enable_networking=True`) requires sudo.

- The library executes `sudo ip ...` and `sudo iptables ...` to configure TAP devices and NAT.
- Users must run the script as root OR have sudo access to run networking commands.
- If you do not have sudo access, create the VM with `enable_networking=False`.

### 2. File operations pause the VM

`upload_file` and `download_file` use `debugfs` on the ext4 filesystem file, so the VM is paused during transfer to avoid corruption. Network connections may time out and real-time processes will be interrupted. For small files, `cat` via `exec_command` avoids the pause, but it's less reliable for binary data.

### 3. Kernel dependencies

VMs need a compatible Linux kernel binary (`vmlinux`).

- By default, it looks at `/var/lib/bandsox/vmlinux`.
- Ensure this file exists, or pass `kernel_path` to `create_vm`.

### 4. Image size

The rootfs size is fixed at build time (Docker export size + overhead). If you need more space, adjust the image generation logic in `image.py`.

### 5. Snapshot compatibility

Restoring a snapshot requires the same kernel and a compatible network config.

- If you move the storage directory, move metadata and snapshots together.
- Snapshots are tied to the exact kernel binary used when they were created.
