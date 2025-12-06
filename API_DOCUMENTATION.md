# BandSox Documentation

BandSox is a Python library for managing Firecracker microVMs. It allows you to create, manage, and snapshot secure sandboxes defined by Docker images.

## Table of Contents

- [Quick Start](#quick-start)
- [Core Concepts](#core-concepts)
- [Usage Guide](#usage-guide)
  - [Initialization](#initialization)
  - [Creating VMs](#creating-vms)
  - [Executing Commands](#executing-commands)
  - [File Operations](#file-operations)
  - [Snapshots](#snapshots)
- [Class Reference](#class-reference)
- [Caveats & Troubleshooting](#caveats--troubleshooting)

## Quick Start

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

## Core Concepts

- **BandSox**: The main controller class that manages the storage, networking, and lifecycle of all VMs.
- **MicroVM**: Represents a single running Firecracker instance. It handles communication with the guest agent and the VMM.
- **Agent**: A lightweight process running inside the guest OS that executes commands and operations requested by the host `MicroVM` object.
- **Rootfs**: The filesystem of the VM, created from a Docker image.

## Usage Guide

### Initialization

The `BandSox` class is your entry point.

```python
from bandsox.core import BandSox

manager = BandSox(storage_dir="/path/to/storage")
```

**Note**: The storage directory will contain sensitive data (images, sockets, metadata). Ensure proper permissions.

### Creating VMs

You can create VMs from existing Docker images or build them from a Dockerfile.

**From Docker Image:**

```python
vm = manager.create_vm(
    docker_image="python:3.9-slim",
    name="my-python-sandbox",
    vcpu=2,
    mem_mib=512,
    enable_networking=True
)
```

**From Dockerfile:**

```python
vm = manager.create_vm_from_dockerfile(
    dockerfile_path="./Dockerfile",
    tag="custom-image:v1",
    name="my-custom-vm"
)
```

### Executing Commands

BandSox provides several ways to run commands.

**1. Blocking Execution (`exec_command`)**
Wait for the command to finish. Good for simple tasks.

```python
code = vm.exec_command("ls -la /", on_stdout=lambda line: print(f"OUT: {line}"), timeout=10)
```

**2. Background Session (`start_session`)**
Run long-running processes. Returns a session ID.

```python
session_id = vm.start_session("sleep 100")
# ... do other things ...
vm.kill_session(session_id)
```

**3. Interactive PTY (`start_pty_session`)**
Allocate a pseudo-terminal. Useful if the application expects a TTY (e.g., shells, interactive CLIs).

```python
session_id = vm.start_pty_session("/bin/sh", cols=80, rows=24)
vm.send_session_input(session_id, "echo Interactive\n")
```

### File Operations

**Important**: Native file operations (`upload_file`, `download_file`, `get_file_contents`) currently use `debugfs` to manipulate the filesystem image directly. **This requires the VM to be temporarily paused** to avoid corruption. This happens automatically but introduces a brief interruption.

```python
# Upload a file
vm.upload_file("./local_script.py", "/app/script.py")

# Download a file
vm.download_file("/app/result.txt", "./result.txt")

# Read file contents (as string)
content = vm.get_file_contents("/etc/hostname")
```

### Snapshots

Snapshots allow you to save the memory and disk state of a running VM and restore it instantly later.

```python
# Create a snapshot
# VM will be paused briefly
snapshot_id = manager.snapshot_vm(vm, snapshot_name="checkpoint-1")

# Use this snapshot ID to start new identical VMs
restored_vm = manager.restore_vm(snapshot_id)
```

## Class Reference

### `BandSox`

| Method | Description |
|--------|-------------|
| `create_vm(docker_image, name=None, vcpu=1, mem_mib=128, ...)` | Creates a new VM instance. |
| `create_vm_from_dockerfile(dockerfile_path, tag, ...)` | Builds an image and creates a VM. |
| `restore_vm(snapshot_id, enable_networking=True)` | Restores a VM from a snapshot. |
| `snapshot_vm(vm, snapshot_name=None)` | create a snapshot of a running VM. |
| `delete_vm(vm_id)` | Stops and deletes a VM and its resources. |
| `list_vms()` | Lists all known VMs. |
| `get_owner(vm_id)` | Returns the `MicroVM` instance. |

### `MicroVM`

| Method | Description |
|--------|-------------|
| `start()`, `stop()`, `pause()`, `resume()` | Lifecycle control. |
| `exec_command(cmd, on_stdout=None, timeout=30)` | Run a command (blocking). |
| `start_session(cmd)` | Run a command (background). |
| `upload_file(local, remote)` | Upload a file (Pauses VM). |
| `download_file(remote, local)` | Download a file (Pauses VM). |
| `get_file_contents(remote)` | Read file content (Pauses VM). |

## Caveats & Troubleshooting

### 1. Root Privileges & Networking

Standard networking setup (`enable_networking=True`) requires **sudo privileges**.

- The library executes `sudo ip ...` and `sudo iptables ...` to configure TAP devices and NAT.
- Users must run the script as root OR have sudo configured to allow these commands without a password.
- If you do not have sudo access, create the VM with `enable_networking=False`.

### 2. File Operations Pause VM

The current implementation of `upload_file` and `download_file` uses `debugfs` on the underlying `ext4` filesystem file.

- **Caveat**: The VM process is **paused** during the file transfer to prevent filesystem corruption.
- **Impact**: Network connections might time out; real-time processes will be interrupted.
- **Alternative**: For small files, consider using `cat` via `exec_command` to avoid pausing, though this is less robust for binary data.

### 3. Kernel Dependencies

The VM boot requires a compatible Linux kernel binary (`vmlinux`).

- By default, it looks at `/var/lib/bandsox/vmlinux`.
- Ensure this file exists, or pass `kernel_path` to `create_vm`.

### 4. Image Size

When creating a VM, the rootfs size is fixed at build time (defaults to Docker export size + overhead). If you need more space, the image generation logic needs to be adjusted (currently in `image.py`, typically minimal size).

### 5. Snapshot Compatibility

Restoring a snapshot requires the **original kernel** and compatible network configuration.

- If you move the backend storage, ensure metadata and snapshots are moved together.
- Snapshots are tied to the exact kernel binary used at creation.
