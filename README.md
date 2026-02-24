# BandSox
<img width="100" height="200" alt="image" src="https://github.com/user-attachments/assets/d80944af-45ac-407d-b2f2-70c95d68be97"/>




BandSox is a Python library and CLI for managing Firecracker microVMs. Create, snapshot, and restore secure sandboxes from Docker images. Runs untrusted code or isolates workloads.

## Features

- Millisecond boot times via Firecracker
- Create VMs from Docker images (requires Python 3 in the image)
- Pause, resume, and snapshot VMs for instant restore
- Web dashboard for managing VMs, snapshots, and terminal sessions
- CLI for all operations
- Python API for scripting and integration
- Vsock file transfers (100-10,000x faster than serial), with automatic serial fallback
- Upload, download, and manage files inside VMs
- Web-based terminal access

## Usage

### Quick start

Create a VM and run Python code:

```python
from bandsox.core import BandSox

bs = BandSox()
vm = bs.create_vm("python:3-alpine", enable_networking=False)

result = vm.exec_python_capture("print('Hello from VM!')")
print(result['stdout'])  # Output: Hello from VM!

vm.stop()
```

### Python API usage

```python
from bandsox.core import BandSox

# Initialize
bs = BandSox()

# Create a VM from a Docker image (which has python preinstalled)
vm = bs.create_vm("python:3-alpine", name="test-vm")
print(f"VM started: {vm.vm_id}")

# Execute a command
exit_code = vm.exec_command("echo Hello World > /root/hello.txt")

# Execute Python code directly in the VM (capture output)
result = vm.exec_python_capture("print('Hello World')")
print(result['stdout'])  # Output: Hello World

# Read a file
content = vm.get_file_contents("/root/hello.txt")
print(content) # Output: Hello World

# Stop the VM
vm.stop()
```

### Web UI

Start the dashboard:

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

## Vsock file transfers

BandSox uses vsock (Virtual Socket) for file transfers between host and guest.

### Performance

Measured transfer speeds:

| File Size | Expected Speed | Expected Time |
|-----------|----------------|----------------|
| 1 MB      | ~50 MB/s      | < 0.1s        |
| 10 MB     | ~80 MB/s      | < 0.2s        |
| 100 MB    | ~100 MB/s     | < 1s          |
| 1 GB      | ~100 MB/s     | < 10s         |

That's 100-10,000x faster than serial-based transfers.

### How it works

- Each VM gets a unique CID (Context ID) and port for vsock communication
- File operations use vsock when available, fall back to serial otherwise
- No VM pause required during transfers
- Vsock bridge is disconnected before snapshots; restores use per-VM vsock isolation to avoid socket collisions

### Restore isolation

Restores mount per-VM vsock paths in a private mount namespace, so multiple restores from the same snapshot don't hit `EADDRINUSE`. The isolation root defaults to `/tmp/bsx` and can be overridden with `BANDSOX_VSOCK_ISOLATION_DIR`.

### Checking vsock status

In a running VM terminal:
```bash
# Check if vsock module is loaded
lsmod | grep vsock
# Should show: virtio_vsock

# Check kernel config
zcat /proc/config.gz | grep VSOCK
# Should see: CONFIG_VIRTIO_VSOCK=y or m
```

### Upgrading from older versions

VMs created before vsock support need to be recreated. See [`VSOCK_MIGRATION.md`](VSOCK_MIGRATION.md) for migration steps.

## Prerequisites

- Linux system with KVM support (bare metal or nested virtualization).
- [Firecracker](https://firecracker-microvm.github.io/) installed and in your PATH (`/usr/bin/firecracker`).
- Python 3.8+.
- `sudo` access (required for setting up TAP devices for networking).
- Vsock kernel module (`virtio-vsock`) in guest kernel for fast file transfers (optional, will fallback to serial if unavailable).

## Installation

### Install from PyPI

Install with pip or uv:

```bash
# Using pip
pip install bandsox

# Using uv (faster)
uv pip install bandsox
```

Then initialize the required artifacts:

```bash
bandsox init --rootfs-url ./bandsox-base.ext4
```

### Install from source

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


## Web UI screenshots
#### Home page
<img width="1564" height="931" alt="image" src="https://github.com/user-attachments/assets/e3bba19c-dba5-4f5d-a5ef-e38df43bbee8" />

---

#### Details page
<img width="1446" height="852" alt="image" src="https://github.com/user-attachments/assets/135512d7-2212-49aa-9454-fa2ae2e918fc" />

##### File browser
###### Browse files inside the VM from the details page.
<img width="1618" height="852" alt="image" src="https://github.com/user-attachments/assets/13191fa2-5b2c-4935-a448-e5d8810a9a1e" />

##### Markdown viewer
###### Click the view button next to any `.md` file to open it in the viewer.
<img width="1261" height="369" alt="image" src="https://github.com/user-attachments/assets/54ca063a-9885-497c-b2be-83ef7180da52" />


---

#### Terminal
###### Open a terminal from the dashboard by clicking the terminal button.
<img width="613" height="219" alt="image" src="https://github.com/user-attachments/assets/2c0148bf-9820-431f-87c0-620c45d4bd03" />



## Architecture

BandSox has four main modules:

- `bandsox.core` -- manages VMs, snapshots, and CID/port allocation.
- `bandsox.vm` -- wraps the Firecracker process; handles config, networking, vsock bridge, and guest interaction.
- `bandsox.agent` -- a small Python agent injected into the VM that runs commands and transfers files (vsock or serial).
- `bandsox.server` -- FastAPI backend for the web dashboard.

### Communication

Vsock (primary):
- VMs talk to the host via `AF_VSOCK` sockets
- Firecracker forwards vsock connections to Unix domain sockets

Serial (fallback):
- Used when vsock is unavailable (e.g., custom kernels without `virtio-vsock`)

### Storage layout

Default: `/var/lib/bandsox` (override with `BANDSOX_STORAGE` env var)

```
├── images/           # Rootfs ext4 images
├── snapshots/        # VM snapshots
├── sockets/          # Firecracker API sockets
├── metadata/         # VM metadata (including vsock_config)
├── cid_allocator.json  # CID allocation state
└── port_allocator.json # Port allocation state
```

## Docs & APIs reference

- Full library, CLI, and HTTP endpoint reference: [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md)
- Vsock migration guide: [`VSOCK_MIGRATION.md`](VSOCK_MIGRATION.md)
- Vsock restoration fix: [`VSOCK_RESTORATION_FIX.md`](VSOCK_RESTORATION_FIX.md)
- REST base path: `http://<host>:<port>/api` (see docs for endpoints such as `/api/vms`, `/api/snapshots`, `/api/vms/{id}/terminal` WebSocket)

## Building a local base rootfs

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

## Storage & artifacts

- Large artifacts (ext4 rootfs images, snapshots, `vmlinux`, CNI binaries) are not tracked in git. `bandsox init` downloads them into `storage/` and `cni/bin/`.
- Default storage path is `/var/lib/sandbox`; override with `BANDSOX_STORAGE` or `--storage`.
- You can pre-seed a base rootfs via `--rootfs-url file://...`, or skip it and let BandSox build per-image rootfs from Docker images on demand.

## Verification & testing

The `verification/` directory has smoke-test scripts:

- `verify_bandsox.py` -- general smoke test
- `verify_file_ops.py` -- file upload/download
- `verify_internet.py` -- network connectivity inside the VM

To run one:

```bash
sudo python3 verification/verify_bandsox.py
```

## License

Apache License 2.0



###### Note: This project wasn't supposed to be made public so it may have artifacts which make no sense. Please open issues so I can remove them.
