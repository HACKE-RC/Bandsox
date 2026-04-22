# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Project overview

BandSox is a Python library and CLI for managing Firecracker microVMs. It creates secure sandboxes from Docker images with fast boot times, snapshotting, and a web dashboard.

## Common commands

```bash
# Install in dev mode
pip install -e .

# Download kernel, CNI plugins, base rootfs
bandsox init --rootfs-url ./bandsox-base.ext4

# Start the server (auth is off unless auth.json exists)
sudo python3 -m bandsox.cli serve --host 0.0.0.0 --port 8000

# Enable auth (off by default, creates auth.json)
sudo bandsox auth init --storage /var/lib/sandbox

# Manage auth
sudo bandsox auth set-password --storage /var/lib/sandbox
bandsox auth create-key my-key
bandsox auth list-keys
bandsox auth revoke-key bsx_k_<id>

# Create a VM
sudo python3 -m bandsox.cli create ubuntu:latest --name my-vm

# Open terminal to a VM
sudo python3 -m bandsox.cli terminal <vm_id>

# Verification tests (require sudo)
sudo python3 verification/verify_bandsox.py
sudo python3 verification/verify_file_ops.py
sudo python3 verification/verify_internet.py
```

## Architecture

### Core components (bandsox/)

- **core.py**: `BandSox` class -- VM lifecycle, storage, snapshots
- **vm.py**: `MicroVM` class -- wraps Firecracker process, config, networking, console multiplexing
- **agent.py**: lightweight Python agent inside guest VMs on ttyS0, runs commands via JSON protocol
- **auth.py**: authentication -- API keys (SHA-256 hashed), HMAC-signed session tokens, rate-limited login, FastAPI dependency. All persistent state in `auth.json`.
- **server.py**: FastAPI web server with REST API, dashboard, and auth middleware
- **cli.py**: command-line interface including `auth` subcommands

### Supporting modules

- **firecracker.py**: client wrapper for Firecracker's REST API via Unix socket
- **network.py**: TAP device setup/cleanup and networking config
- **cni.py**: CNI runtime for bridge networking with IPAM
- **image.py**: Docker image to ext4 rootfs conversion
- **runner.py**: VM runner daemon for managing Firecracker processes

### Authentication

Auth is off by default. Enable it with `bandsox auth init`, which creates `auth.json` in the storage directory. When `auth.json` exists, all endpoints require auth. When it doesn't, everything is open.

Two auth mechanisms, checked by a single FastAPI dependency:

1. **API keys**: `Authorization: Bearer <key>` header. For CLI, SDK, and direct API calls. Keys stored as SHA-256 hashes in `{storage_dir}/auth.json`.
2. **Session cookies**: `bandsox_session` cookie. For the browser dashboard. Created via `/api/auth/login` with the admin password. Sessions are HMAC-signed tokens (expiry timestamp + SHA-256 signature), so they survive server restarts. The signing secret is in `auth.json`.

WebSocket terminal uses a `token=` query parameter since browser WebSocket API can't send custom headers.

CLI credentials are stored at `~/.bandsox/credentials`.

### Host-guest communication

The agent (`agent.py`) runs inside each VM and talks to the host over serial console (ttyS0) using a JSON protocol. Commands are sent as JSON messages with types like `exec`, `read_file`, `write_file`, and the agent responds with structured results.

### Networking

- TAP devices with CNI bridge networking
- IP allocation via SHA256 hash of VM ID
- NAT/masquerading via iptables
- Network namespaces for isolation

### Storage layout

Default storage path: `/var/lib/bandsox` (override with `BANDSOX_STORAGE` env var)
- `images/`: rootfs ext4 images
- `snapshots/`: VM snapshots
- `sockets/`: Firecracker API sockets
- `metadata/`: VM metadata JSON files
- `auth.json`: API key hashes and admin password hash

Large artifacts (vmlinux kernel, CNI binaries, rootfs images) are downloaded by `bandsox init`, not tracked in git.

## Prerequisites

- Linux with KVM support
- Firecracker binary at `/usr/bin/firecracker`
- Python 3.8+
- sudo access (required for TAP device setup)
- Docker (for building rootfs from images)

## Code style

Follow PEP 8. Use `black` for formatting.
