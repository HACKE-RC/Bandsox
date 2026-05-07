import os
import sys
import argparse
import json
import logging
import signal
import threading
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
    # Vsock config for restored VMs. The runner lives inside the mount
    # namespace (unshare -m) so it can see the Firecracker vsock UDS at
    # the baked path. The caller (athena) is outside the namespace and
    # can't bind a listener there. So the runner must start the
    # VsockHostListener itself after Firecracker creates the socket.
    parser.add_argument(
        "--vsock-config",
        type=str,
        default=None,
        help="JSON blob describing vsock configuration for a restored VM",
    )
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

    if args.vsock_config:
        try:
            vsock_config = json.loads(args.vsock_config)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid --vsock-config JSON: {e}")
            sys.exit(2)
        if vsock_config.get("enabled"):
            vm.vsock_enabled = True
            vm.vsock_cid = vsock_config.get("cid")
            vm.vsock_port = vsock_config.get("port")
            host_path = vsock_config.get("host_uds_path") or vsock_config.get("uds_path")
            if host_path:
                vm.vsock_socket_path = host_path
            baked = vsock_config.get("baked_uds_path") or vsock_config.get("uds_path")
            if baked:
                vm.vsock_baked_path = baked
            if vm.vsock_port is not None:
                vm.env_vars["BANDSOX_VSOCK_PORT"] = str(vm.vsock_port)
            logger.info(
                f"Recorded vsock config: cid={vm.vsock_cid}, port={vm.vsock_port}, "
                f"host_path={vm.vsock_socket_path}"
            )

    try:
        vm.start_process()
        logger.info(f"VM {args.vm_id} started. Waiting for completion...")
        console_sock = vm.console_socket_path
        logger.info(f"Console multiplexer listening on {console_sock}")

        # Start the VsockHostListener as soon as the Firecracker vsock UDS
        # appears. The caller (core.restore_vm) does load_snapshot followed
        # by resume_vm; the UDS appears during load_snapshot, so binding
        # here races ahead of resume and the guest's first connection.
        #
        # Run the wait-and-bind in a background thread so the runner's main
        # loop can continue to monitor Firecracker in case it crashes
        # before the vsock socket is ever created.
        if vm.vsock_enabled and vm.vsock_socket_path and vm.vsock_port:
            _start_vsock_listener_async(vm)

        # Monitor Firecracker. If it exits, we exit too.
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


def _start_vsock_listener_async(vm):
    """Wait for Firecracker's vsock UDS to appear, then bind our listener.

    Retries the bind up to a few times to survive transient errors. If the
    accept loop ever dies we restart it so the listener stays healthy for
    the lifetime of the VM.
    """
    def _worker():
        vsock_path = vm.vsock_socket_path
        port = vm.vsock_port
        listener_path = f"{vsock_path}_{port}"

        logger.info(f"Waiting for Firecracker vsock UDS at {vsock_path}")
        max_wait_s = 60
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            if os.path.exists(vsock_path):
                break
            if vm.process and vm.process.poll() is not None:
                logger.warning("Firecracker exited before vsock UDS appeared")
                return
            time.sleep(0.05)
        else:
            logger.warning(
                f"Vsock UDS not found at {vsock_path} after {max_wait_s}s; "
                "guest reads will fall back to serial console"
            )
            return

        # Bind the listener with a few retries — setup_vsock_listener is
        # idempotent: it unlinks stale files before bind.
        bind_attempts = 5
        for attempt in range(1, bind_attempts + 1):
            try:
                vm.setup_vsock_listener(port)
                logger.info(
                    f"Vsock listener bound: port={port}, path={listener_path}"
                )
                # The agent inside the snapshot was already "ready" when
                # we snapshotted, and after resume_vm it's running again.
                # The runner's MicroVM never sees a fresh "ready" event
                # over stdout though, so its agent_ready stays False —
                # which makes FastWriteServer (and any future RPC) refuse
                # to dispatch with "Agent not ready". Mark it ready now
                # that we have a working vsock listener.
                vm.agent_ready = True
                break
            except Exception as e:
                logger.warning(
                    f"Vsock listener bind attempt {attempt}/{bind_attempts} failed: {e}"
                )
                time.sleep(0.2 * attempt)
        else:
            logger.error(
                f"Vsock listener failed to bind after {bind_attempts} attempts"
            )
            return

        # Supervise the accept loop — if the listener thread dies for any
        # reason, rebind so the guest doesn't get permanently stuck on
        # serial. The accept loop sets running=False on socket errors
        # but does NOT restart; we do that here.
        supervisor = threading.Thread(
            target=_supervise_listener, args=(vm, port), daemon=True,
            name=f"vsock-supervisor-{port}",
        )
        supervisor.start()

    t = threading.Thread(target=_worker, daemon=True, name="vsock-bind")
    t.start()


def _supervise_listener(vm, port):
    """Restart the vsock listener if its accept thread dies.

    The VsockHostListener has a single accept loop thread that can break
    out on OSError. Without supervision the socket file stays present
    (so the guest thinks vsock exists) but nobody answers, causing
    `Connection reset by peer` on every attempt.
    """
    while True:
        listener = vm.vsock_listener
        if listener is None:
            # Listener was cleaned up (VM stop)
            return
        accept_thread = listener.accept_thread
        if accept_thread is None or not accept_thread.is_alive():
            if not listener.running:
                # Clean shutdown, don't restart
                return
            logger.warning(
                f"Vsock accept loop died on port {port}; restarting listener"
            )
            try:
                listener.stop()
            except Exception:
                pass
            try:
                vm.setup_vsock_listener(port)
                logger.info(f"Vsock listener restarted on port {port}")
            except Exception as e:
                logger.error(f"Vsock listener restart failed: {e}")
                time.sleep(5)
                continue
        if vm.process and vm.process.poll() is not None:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
