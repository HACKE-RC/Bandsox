import time
import logging
import os
from bandsox.core import BandSox

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run_demo")

def main():
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage")
    
    logger.info("Creating Demo VM...")
    vm = bs.create_vm("python:alpine", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=False)
    
    logger.info(f"VM {vm.vm_id} started.")
    logger.info("You can now inspect it in the dashboard at http://localhost:8000")
    logger.info("Press Ctrl+C to stop the VM and exit.")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping VM...")
        vm.stop()

if __name__ == "__main__":
    main()
