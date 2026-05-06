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
  - `agent.py`: Guest agent code.
  - `server.py`: Web server.
- `verification/`: Verification and test scripts.
- `scripts/`: Utility scripts.

## Running tests

Unit tests:

```bash
uv run python -m pytest -q
```

Verification scripts live in `verification/`. Most need `sudo` (network devices,
KVM, Firecracker).

```bash
sudo env PATH=$PATH uv run python verification/verify_bandsox.py
sudo env PATH=$PATH uv run python verification/verify_go_agent.py
```

Benchmarking:

```bash
sudo env PATH=$PATH uv run python verification/benchmark_go_agent.py
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
