# Contributing to BandSox

## Development setup

1.  Clone the repo.
2.  Install development dependencies (e.g., `pytest`, `black`, `isort`).
3.  Ensure you have `firecracker` installed.
4.  Fetch runtime artifacts with `bandsox init` (kernel, CNI, optional base rootfs). These are not committed to git.

## Project structure

- `bandsox/`: Main package source code.
  - `core.py`: Main entry point.
  - `vm.py`: MicroVM management.
  - `agent/main.go`: Go guest agent (built into VM images as `bandsox-agent`).
  - `agent.py`: legacy Python agent (tests only; not used in production images).
  - `server.py`: Web server.
- `tests/`: Pytest suite (`test_*.py`) plus sudo-only smoke and benchmark
  scripts (`smoke_*.py`, `benchmark_*.py`) that boot real microVMs.
- `scripts/`: Utility scripts.

## Running tests

Unit tests:

```bash
uv run python -m pytest -q
```

Smoke scripts boot real Firecracker VMs and may prompt for `sudo` (network devices,
KVM, Firecracker). They are not picked up by pytest.

```bash
uv run python tests/smoke_bandsox.py
uv run python tests/smoke_go_agent.py
```

Benchmarking:

```bash
uv run python tests/benchmark_go_agent.py
```

On a typical dev machine the benchmark reports ~2.3ms mean `exec_command("true")`
and ~190 MiB/s upload + up to ~1 GiB/s download for an 8 MiB file (vsock raw path).

## Code style

Follow PEP 8. Use `black` for formatting.

## Pull requests

1.  Fork the repo.
2.  Create a feature branch.
3.  Commit your changes.
4.  Push to your fork.
5.  Submit a Pull Request.
