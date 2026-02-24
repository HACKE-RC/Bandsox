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

Verification scripts live in `verification/`. They need `sudo` because they create network devices.

```bash
sudo python3 verification/verify_bandsox.py
```

## Code style

Follow PEP 8. Use `black` for formatting.

## Pull requests

1.  Fork the repo.
2.  Create a feature branch.
3.  Commit your changes.
4.  Push to your fork.
5.  Submit a Pull Request.
