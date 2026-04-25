import os
import sys
import argparse
import logging
import signal
import time
from pathlib import Path
from .vm import MicroVM, ConsoleMultiplexer
from .core import BandSox

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("bandsox-runner")


def handle_signals(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="BandSox VM Runner")
    parser.add_argument("vm_id", type=str, help="VM ID")
    parser.add_argument(
        "--socket-path", type=str, required=True, help="Path for Firecracker API socket"
    )
    parser.add_argument("--netns", type=str, help="Network namespace to run in")
    parser.add_argument(
        "--vsock-isolation-dir",
        type=str,
        help="Host directory for per-VM vsock isolation",
    )
    # NOTE: the runner deliberately does NOT manage the vsock listener.
    # Firecracker routes guest AF_VSOCK connections to a Unix socket at
    # <uds_path>_<port>; whichever process calls download_file /
    # register_pending_upload needs to own that socket so it can steer
    # incoming files to the caller's local_path. That's always the
    # BandSox caller (the parent process), not the runner, so we leave
    # listener lifecycle to restore_vm().
    args = parser.parse_args()

    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_signals)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    logger.info(f"Starting runner for VM {args.vm_id}")

    vm = MicroVM(args.vm_id, args.socket_path, netns=args.netns)
    if args.vsock_isolation_dir:
        vm.vsock_isolation_dir = args.vsock_isolation_dir

    try:
        vm.start_process()
        logger.info(f"VM {args.vm_id} started. Waiting for completion...")
        console_sock = vm.console_socket_path
        logger.info(f"Console multiplexer listening on {console_sock}")

        while True:
            if vm.process and vm.process.poll() is not None:
                logger.info(
                    f"Firecracker process exited with code {vm.process.returncode}"
                )
                break
            time.sleep(1)

    except Exception:
        logger.exception("Runner failed")
        if vm.process:
            vm.process.kill()
    finally:
        if vm:
            vm.stop()


if __name__ == "__main__":
    main()
