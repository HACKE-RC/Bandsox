# BandSox
<img width="60" height="200" alt="image" src="https://github.com/user-attachments/assets/d80944af-45ac-407d-b2f2-70c95d68be97"/>




BandSox is a fast, lightweight Python library and CLI for managing Firecracker microVMs. It provides a simple interface to create, manage, and interact with secure sandboxes, making it easy to run untrusted code or isolate workloads.

## Features

- **Fast Boot Times**: Leverages Firecracker's speed to start VMs in milliseconds.
- **Docker Image Support**: Create VMs directly from Docker images (requires Python 3 installed in the image).
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
    git clone https://github.com/HACKE-RC/Bandsox.git
    cd bandsox
    ```

2. Install dependencies:

    ```bash
    pip install -e .
    ```

3. Initialize required artifacts (kernel, CNI plugins, optional base rootfs):

    ```bash
    # Use a locally-built rootfs (see instructions below)
    bandsox init --rootfs-url ./bandsox-base.ext4
    ```

    This downloads:
    - `vmlinux` (Firecracker kernel)
    - CNI plugins (from the official upstream releases, e.g.
      `https://github.com/containernetworking/plugins/releases/download/v1.5.1/cni-plugins-linux-amd64-v1.5.1.tgz`)
      into `cni/bin/` (or your chosen `--cni-dir`)
    - (Optional) a base rootfs `.ext4` into `storage/images/` when `--rootfs-url` is provided

    Default URLs are provided for kernel and CNI. For the rootfs, build one locally (instructions below) and point `--rootfs-url` to a local path (or `file://` URL). Use `--skip-*` flags to omit specific downloads or `--force` to re-download.

## Usage

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

### Web UI

Start the web dashboard:

```bash
sudo python3 -m bandsox.cli serve --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000` to access the dashboard.

### CLI

BandSox includes a CLI tool `bandsox` (or `python -m bandsox.cli`).

**Create a VM:**

```bash
sudo python3 -m bandsox.cli create ubuntu:latest --name my-vm
```

**Open a Terminal:**

```bash
sudo python3 -m bandsox.cli terminal <vm_id>
```

**Start the Web Dashboard:**

```bash
sudo python3 -m bandsox.cli serve --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000` to access the dashboard.

## Web UI Screenshots
#### Home Page
<img width="1564" height="931" alt="image" src="https://github.com/user-attachments/assets/e3bba19c-dba5-4f5d-a5ef-e38df43bbee8" />

---

#### Details Page
<img width="1446" height="852" alt="image" src="https://github.com/user-attachments/assets/135512d7-2212-49aa-9454-fa2ae2e918fc" />

##### File browser
###### The details page has a file browser which you can use to explore the files inside the VM.
<img width="1618" height="852" alt="image" src="https://github.com/user-attachments/assets/13191fa2-5b2c-4935-a448-e5d8810a9a1e" />

##### Markdown viewer
###### Markdown files have a view button next to them, which opens a markdown viewer
<img width="1261" height="369" alt="image" src="https://github.com/user-attachments/assets/54ca063a-9885-497c-b2be-83ef7180da52" />


---

#### Terminal
###### The webui has a terminal which can be accessed by clicking on the terminal button
<img width="613" height="219" alt="image" src="https://github.com/user-attachments/assets/2c0148bf-9820-431f-87c0-620c45d4bd03" />



## Architecture

BandSox consists of several components:

- **Core (`bandsox.core`)**: High-level manager for VMs and snapshots.
- **VM (`bandsox.vm`)**: Wrapper around the Firecracker process, handling configuration, network, and interaction.
- **Agent (`bandsox.agent`)**: A lightweight Python agent injected into the VM to handle command execution and file operations.
- **Server (`bandsox.server`)**: FastAPI-based backend for the web dashboard.

## Docs & APIs

- Full library, CLI, and HTTP endpoint reference: [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md)
- REST base path: `http://<host>:<port>/api` (see docs for endpoints such as `/api/vms`, `/api/snapshots`, `/api/vms/{id}/terminal` WebSocket)

## Building a local base rootfs (no hosting required)

Build a minimal ext4 from a Docker image and keep it local:

```bash
IMG=alpine:latest          # pick a base image with python if needed
OUT=bandsox-base.ext4
SIZE_MB=512                # increase for more disk
TMP=$(mktemp -d)

docker pull "$IMG"
CID=$(docker create "$IMG")
docker export "$CID" -o "$TMP/rootfs.tar"
docker rm "$CID"

dd if=/dev/zero of="$OUT" bs=1M count=$SIZE_MB
mkfs.ext4 -F "$OUT"
mkdir -p "$TMP/mnt"
sudo mount -o loop "$OUT" "$TMP/mnt"
sudo tar -xf "$TMP/rootfs.tar" -C "$TMP/mnt"

cat <<'EOF' | sudo tee "$TMP/mnt/init" >/dev/null
#!/bin/sh
export PATH=/usr/local/bin:/usr/bin:/bin:/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
P=$(command -v python3 || command -v python)
[ -z "$P" ] && exec /usr/local/bin/agent.py
exec "$P" /usr/local/bin/agent.py
EOF
sudo chmod +x "$TMP/mnt/init"

sudo mkdir -p "$TMP/mnt/usr/local/bin"
sudo cp bandsox/agent.py "$TMP/mnt/usr/local/bin/agent.py"
sudo chmod 755 "$TMP/mnt/usr/local/bin/agent.py"

sudo umount "$TMP/mnt"
sudo e2fsck -fy "$OUT"
sudo resize2fs -M "$OUT"   # optional: shrink to minimum
rm -rf "$TMP"
```

Use it locally with `bandsox init --rootfs-url ./bandsox-base.ext4` (or `file://$PWD/bandsox-base.ext4`).

Alternative: skip providing a base rootfs entirely—BandSox can build per-image rootfs on demand from Docker images when you call `bandsox create <image>`.

## Storage & Artifacts

- Large artifacts (ext4 rootfs images, snapshots, `vmlinux`, CNI binaries) are **not** tracked in git; `bandsox init` downloads them into `storage/` and `cni/bin/` (or a directory you pass via `--cni-dir` pointing at the official CNI release tarball).
- Default storage path is `/var/lib/sandbox`; override with `BANDSOX_STORAGE` or `--storage` when running the server.
- To pre-seed a base rootfs, build it locally and reference it via `--rootfs-url file://...`; otherwise, create VMs from Docker images on demand (no prebuilt rootfs needed).

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

Apache License 2.0



###### Note: This project wasn't supposed to be made public so it may have artifacts which make no sense. Please open issues so I can remove them.
