# Contributing to BandSox

We welcome contributions to BandSox!

## Development Setup

1.  Clone the repo.
2.  Install development dependencies (e.g., `pytest`, `black`, `isort`).
3.  Ensure you have `firecracker` installed.
4.  Fetch runtime artifacts with `bandsox init` (kernel, CNI, optional base rootfs). These are not committed to git.

## Project Structure

- `bandsox/`: Main package source code.
  - `core.py`: Main entry point.
  - `vm.py`: MicroVM management.
  - `agent.py`: Guest agent code.
  - `server.py`: Web server.
- `verification/`: Verification and test scripts.
- `scripts/`: Utility scripts.

## Running Tests

Currently, we use verification scripts in `verification/`.
Run them with `sudo` as they require network device creation.

```bash
sudo python3 verification/verify_bandsox.py
```

## Code Style

Please follow PEP 8. We recommend using `black` for formatting.

## Pull Requests

1.  Fork the repo.
2.  Create a feature branch.
3.  Commit your changes.
4.  Push to your fork.
5.  Submit a Pull Request.
