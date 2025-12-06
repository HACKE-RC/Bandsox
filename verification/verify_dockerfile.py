import sys
import time
import logging
from bandsox.core import BandSox
import time
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_dockerfile")

def main():
    import os
    from pathlib import Path
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage_verify_dockerfile")
    
    logger.info("Creating VM from Dockerfile...")
    vm = bs.create_vm_from_dockerfile("verification/Dockerfile", tag="bandsox-test-image-v5", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=False)
    
    try:
        logger.info(f"VM {vm.vm_id} started. Waiting for boot...")
        time.sleep(15)
        
        logger.info("Verifying build artifact...")
        def print_output(data):
            logger.info(f"VM: {data.strip()}")
            
        exit_code = vm.exec_command("whoami", on_stdout=print_output, on_stderr=print_output)
        if exit_code != 0:
            logger.error("Build artifact missing.")
            sys.exit(1)
            
        logger.info("Dockerfile verification PASSED!")
        bs.snapshot_vm(vm, "arch-box")
        # vm.stop()
        print(f"VM {vm.vm_id} kept running for debugging.")
        
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        vm.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
