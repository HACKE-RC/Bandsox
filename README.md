# BandSox
<img width="200" alt="BandSox logo" src="https://github.com/user-attachments/assets/d80944af-45ac-407d-b2f2-70c95d68be97"/>

Python library and CLI for managing Firecracker microVMs. Create, snapshot, and restore sandboxes from Docker images. Runs untrusted code or isolates workloads.

## Features

- Millisecond boot times via Firecracker
- Create VMs from Docker images (guest agent is a static Go binary; Python is not required in the image)
- Pause, resume, and snapshot VMs for instant restore
- Web dashboard with login, API key management, and terminal sessions
- CLI for all operations, including auth management
- Python API for scripting and integration
- TypeScript SDK for Node.js
- Vsock file transfers (100-10,000x faster than serial), with automatic serial fallback
- Optional authentication: API keys for programmatic access, session cookies for the dashboard. Off by default.

## Usage

### Quick start

Create a VM and run Python code:

```python
from bandsox.core import BandSox

bs = BandSox()
vm = bs.create_vm("python:3-alpine", enable_networking=False)

result = vm.exec_python_capture("print('Hello from VM!')")
print(result['stdout'])  # Hello from VM!

vm.stop()
```

### Python API

```python
from bandsox.core import BandSox

bs = BandSox()

# Create a VM from a Docker image (needs python preinstalled)
vm = bs.create_vm("python:3-alpine", name="test-vm")
print(f"VM started: {vm.vm_id}")

# Run a command
exit_code = vm.exec_command("echo Hello World > /root/hello.txt")

# Run Python code directly inside the VM
result = vm.exec_python_capture("print('Hello World')")
print(result['stdout'])  # Hello World

# Read a file
content = vm.get_file_contents("/root/hello.txt")
print(content)  # Hello World

vm.stop()
```

### Remote server usage

If the BandSox server is already running somewhere, point the Python client at it:

```python
from bandsox.core import BandSox

# With authentication
bs = BandSox("http://localhost:8000", headers={"Authorization": "Bearer bsx_your_key_here"})
vm = bs.create_vm("python:3-alpine", enable_networking=False)

result = vm.exec_python_capture("print('Hello from the server')")
print(result["stdout"])

vm.stop()
```

### Web dashboard

Start the server:

```bash
sudo python3 -m bandsox.cli serve --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000` to access the dashboard. Authentication is off by default -- see the [Authentication](#authentication) section to enable it.

### Authentication

Auth is off by default. All endpoints are open until you explicitly enable it.

To turn it on:

```bash
sudo bandsox auth init --storage /var/lib/sandbox
```

This creates `auth.json` in the storage directory and prints an admin password and API key. Save both -- the API key is only shown once. The CLI will offer to save the key to `~/.bandsox/credentials` for you.

Once enabled, BandSox uses two auth methods:

**API keys** for programmatic access (CLI, SDK, direct HTTP calls). Pass them as `Authorization: Bearer <key>` headers.

**Session cookies** for the browser dashboard. Log in with the admin password at `/login`. Sessions are signed tokens that survive server restarts.

To disable auth again, delete `auth.json` from the storage directory.

#### CLI auth commands

```bash
# Enable auth (generates password + API key)
sudo bandsox auth init --storage /var/lib/sandbox

# Set or reset the admin password
sudo bandsox auth set-password --storage /var/lib/sandbox

# Create a new API key
bandsox auth create-key my-key

# List and revoke keys
bandsox auth list-keys
bandsox auth revoke-key bsx_k_<id>
```

#### SDK auth

TypeScript:

```ts
const bs = new BandSox({
  baseUrl: "http://localhost:8000",
  headers: { Authorization: "Bearer bsx_your_key_here" },
});
```

Python:

```python
bs = BandSox("http://localhost:8000", headers={"Authorization": "Bearer bsx_your_key_here"})
```

### CLI

BandSox includes a CLI tool `bandsox` (or `python -m bandsox.cli`).

```bash
# Create a VM
sudo python3 -m bandsox.cli create ubuntu:latest --name my-vm

# Open a terminal
sudo python3 -m bandsox.cli terminal <vm_id>

# Start the server
sudo python3 -m bandsox.cli serve --host 0.0.0.0 --port 8000
```

## Vsock file transfers

BandSox uses vsock (Virtual Socket) for file transfers between host and guest.

### Performance

| File size | Speed    | Time   |
|-----------|----------|--------|
| 1 MB      | ~50 MB/s | < 0.1s |
| 10 MB     | ~80 MB/s | < 0.2s |
| 100 MB    | ~100 MB/s| < 1s   |
| 1 GB      | ~100 MB/s| < 10s  |

That's 100-10,000x faster than serial-based transfers.

### How it works

- Each VM gets a unique CID (Context ID) and port for vsock
- File operations use vsock when available, fall back to serial otherwise
- No VM pause required during transfers
- Vsock bridge is disconnected before snapshots; restores use per-VM isolation to avoid socket collisions

### Restore isolation

Restores mount per-VM vsock paths in a private mount namespace, so multiple restores from the same snapshot don't hit `EADDRINUSE`. The isolation root defaults to `/tmp/bsx` (override with `BANDSOX_VSOCK_ISOLATION_DIR`).

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

VMs created before vsock support need to be recreated. See [`docs/VSOCK_MIGRATION.md`](docs/VSOCK_MIGRATION.md) for details.

## Prerequisites

- Linux with KVM support (bare metal or nested virtualization)
- [Firecracker](https://firecracker-microvm.github.io/) installed at `/usr/bin/firecracker`
- Python 3.8+
- `sudo` access (required for TAP device networking)
- Vsock kernel module (`virtio-vsock`) in the guest kernel for fast file transfers (optional, falls back to serial)

## Installation

### From PyPI

```bash
# Using pip
pip install bandsox

# Using uv
uv pip install bandsox
```

Then initialize the required artifacts:

```bash
bandsox init --rootfs-url ./bandsox-base.ext4
```

### From source

1. Clone the repo:

    ```bash
    git clone https://github.com/HACKE-RC/Bandsox.git
    cd bandsox
    ```

2. Install:

    ```bash
    pip install -e .
    ```

3. Initialize artifacts (kernel, CNI plugins, optional base rootfs):

    ```bash
    bandsox init --rootfs-url ./bandsox-base.ext4
    ```

    This downloads:
    - `vmlinux` (Firecracker kernel)
    - CNI plugins into `cni/bin/`
    - (Optional) a base rootfs `.ext4` into `storage/images/` when `--rootfs-url` is provided

    Default URLs are provided for kernel and CNI. For the rootfs, build one locally (instructions below) and point `--rootfs-url` to a local path or `file://` URL. Use `--skip-*` flags to omit specific downloads or `--force` to re-download.


## Architecture

BandSox has four main modules:

- `bandsox.core` -- manages VMs, snapshots, and CID/port allocation.
- `bandsox.vm` -- wraps the Firecracker process; handles config, networking, vsock bridge, and guest interaction.
- `bandsox.agent` -- a small Python agent injected into the VM that runs commands and transfers files (vsock or serial).
- `bandsox.server` -- FastAPI backend for the web dashboard and REST API, with built-in authentication.
- `bandsox.auth` -- optional authentication. When `auth.json` exists, enforces API key and session auth. Stores hashed keys and a signing secret. Sessions are HMAC-signed tokens (no server-side state). Rate-limits login attempts.

### Communication

Vsock (primary):
- VMs talk to the host via `AF_VSOCK` sockets
- Firecracker forwards vsock connections to Unix domain sockets

Serial (fallback):
- Used when vsock is unavailable (e.g., custom kernels without `virtio-vsock`)

### Storage layout

Default: `/var/lib/bandsox` (override with `BANDSOX_STORAGE` env var)

```
├── images/               # Rootfs ext4 images
├── snapshots/            # VM snapshots
├── sockets/              # Firecracker API sockets
├── metadata/             # VM metadata (including vsock_config)
├── auth.json             # API key hashes, admin password hash, session signing secret
├── cid_allocator.json    # CID allocation state
└── port_allocator.json   # Port allocation state
```

## Docs and API reference

- Full library, CLI, and HTTP endpoint reference: [`docs/API.md`](docs/API.md)
- Vsock migration guide: [`docs/VSOCK_MIGRATION.md`](docs/VSOCK_MIGRATION.md)
- Vsock restoration fix: [`VSOCK_RESTORATION_FIX.md`](VSOCK_RESTORATION_FIX.md)
- REST base path: `http://<host>:<port>/api` (all endpoints require auth -- see API docs)

## Building a local base rootfs

Build a minimal ext4 from a Docker image:

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

Use it with `bandsox init --rootfs-url ./bandsox-base.ext4`.

You can also skip the base rootfs entirely -- BandSox builds per-image rootfs on demand from Docker images when you call `bandsox create <image>`.

## Storage and artifacts

- Large artifacts (ext4 rootfs images, snapshots, `vmlinux`, CNI binaries) are not tracked in git. `bandsox init` downloads them into `storage/` and `cni/bin/`.
- Default storage path is `/var/lib/sandbox`; override with `BANDSOX_STORAGE` or `--storage`.
- You can pre-seed a base rootfs via `--rootfs-url file://...`, or let BandSox build per-image rootfs from Docker images on demand.

## Verification and testing

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
