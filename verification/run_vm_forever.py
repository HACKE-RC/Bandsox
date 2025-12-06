import time
import logging
import os
from bandsox.core import BandSox

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run_vm_forever")

def main():
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage")
    
    logger.info("Creating VM...")
    vm = bs.create_vm("python:alpine", name="python-forever", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=False)
    
    logger.info(f"VM {vm.vm_id} started. Running forever. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping VM...")
        vm.stop()

if __name__ == "__main__":
    main()
