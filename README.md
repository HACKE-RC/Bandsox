# BandSox

BandSox is a fast, lightweight Python library and CLI for managing Firecracker microVMs. It provides a simple interface to create, manage, and interact with secure sandboxes, making it easy to run untrusted code or isolate workloads.

## Features

- **Fast Boot Times**: Leverages Firecracker's speed to start VMs in milliseconds.
- **Docker Image Support**: Create VMs directly from Docker images.
- **Snapshotting**: Pause, resume, and snapshot VMs for instant restoration.
- **Web Dashboard**: Visual interface to manage VMs, snapshots, and view terminal sessions.
- **CLI Tool**: Comprehensive command-line interface for all operations.
- **Python API**: Easy-to-use Python library for integration into your own applications.
- **File Operations**: Upload, download, and manage files within the VM.
- **Terminal Access**: Interactive web-based terminal for running VMs.

## Prerequisites

- Linux system with KVM support (bare metal or nested virtualization).
- [Firecracker](https://firecracker-microvm.github.io/) installed and in your PATH (`/usr/bin/firecracker`).
- Python 3.8+.
- `sudo` access (required for setting up TAP devices for networking).

## Installation

1. Clone the repository:

    ```bash
    git clone https://github.com/yourusername/bandsox.git
    cd bandsox
    ```

2. Install dependencies:

    ```bash
    pip install -e .
    ```

3. Download the Linux kernel (required for Firecracker):

    ```bash
    ./scripts/download_kernel.sh
    ```

    (Note: Ensure `download_kernel.sh` is executable and run it to fetch `vmlinux`).

## Usage

### CLI

BandSox includes a CLI tool `bandsox` (or `python -m bandsox.cli`).

**Start the Web Dashboard:**

```bash
sudo python3 -m bandsox.cli serve --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000` to access the dashboard.

**Create a VM:**

```bash
sudo python3 -m bandsox.cli create ubuntu:latest --name my-vm
```

**Open a Terminal:**

```bash
sudo python3 -m bandsox.cli terminal <vm_id>
```

### Python API

```python
from bandsox.core import BandSox

# Initialize
bs = BandSox()

# Create a VM from a Docker image
vm = bs.create_vm("alpine:latest", name="test-vm")
print(f"VM started: {vm.vm_id}")

# Execute a command
exit_code = vm.exec_command("echo Hello World > /root/hello.txt")

# Read a file
content = vm.get_file_contents("/root/hello.txt")
print(content) # Output: Hello World

# Stop the VM
vm.stop()
```

## Architecture

BandSox consists of several components:

- **Core (`bandsox.core`)**: High-level manager for VMs and snapshots.
- **VM (`bandsox.vm`)**: Wrapper around the Firecracker process, handling configuration, network, and interaction.
- **Agent (`bandsox.agent`)**: A lightweight Python agent injected into the VM to handle command execution and file operations.
- **Server (`bandsox.server`)**: FastAPI-based backend for the web dashboard.

## Verification & Testing

The `verification/` directory contains scripts to verify various functionalities:

- `verify_bandsox.py`: General smoke test.
- `verify_file_ops.py`: Tests file upload/download.
- `verify_internet.py`: Tests network connectivity inside the VM.

To run a verification script:

```bash
sudo python3 verification/verify_bandsox.py
```

## License

MIT
