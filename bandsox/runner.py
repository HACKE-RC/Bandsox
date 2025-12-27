
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
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.StreamHandler(sys.stdout),
                        # logging.FileHandler("/var/log/bandsox-runner.log") # Optional
                    ])
logger = logging.getLogger("bandsox-runner")

def handle_signals(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="BandSox VM Runner")
    parser.add_argument("vm_id", type=str, help="VM ID")
    parser.add_argument("--socket-path", type=str, required=True, help="Path for Firecracker API socket")
    parser.add_argument("--netns", type=str, help="Network namespace to run in")
    
    args = parser.parse_args()
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_signals)
    # Ignore SIGINT (Ctrl-C) and SIGHUP (Terminal closed) to behave like a daemon
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    
    logger.info(f"Starting runner for VM {args.vm_id}")
    
    # Instantiate MicroVM (lite version, we just need start_process logic mostly)
    # But MicroVM class does a lot. We can use it.
    
    vm = MicroVM(args.vm_id, args.socket_path, netns=args.netns)
    
    try:
        # Start the process (Firecracker + Multiplexer)
        vm.start_process()
        
        logger.info(f"VM {args.vm_id} started. Waiting for completion...")
        
        # Determine console socket path for logging
        console_sock = vm.console_socket_path
        logger.info(f"Console multiplexer listening on {console_sock}")
        
        # Keep alive loop
        while True:
            if vm.process and vm.process.poll() is not None:
                logger.info(f"Firecracker process exited with code {vm.process.returncode}")
                break
            time.sleep(1)
            
    except Exception as e:
        logger.exception("Runner failed")
        if vm.process:
            vm.process.kill()
    finally:
        if vm:
            vm.stop()

if __name__ == "__main__":
    main()
