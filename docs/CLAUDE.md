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

# Unit tests
uv run pytest

# Smoke scripts (require sudo, boot real microVMs)
sudo python3 tests/smoke_bandsox.py
sudo python3 tests/smoke_go_agent.py
sudo python3 tests/smoke_internet.py
```

## Architecture

### Core components (bandsox/)

- **core.py**: `BandSox` class -- VM lifecycle, storage, snapshots
- **vm.py**: `MicroVM` class -- wraps Firecracker process, config, networking, console multiplexing
- **agent/main.go**: static Go guest agent inside VMs on ttyS0, runs commands/file ops via JSON protocol (+ vsock fast paths)
- **agent.py**: legacy Python agent kept for compatibility/testing (not required in guest images)
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

See [AUTHENTICATION.md](AUTHENTICATION.md) for the full model. TL;DR: off by
default, enable with `bandsox auth init`, two mechanisms (Bearer API keys
and HMAC-signed session cookies) checked by a single FastAPI dependency,
WebSocket terminal uses a `token=` query parameter.

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
