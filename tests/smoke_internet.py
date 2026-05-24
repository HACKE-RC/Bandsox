import sys
import time
import logging
from bandsox.core import BandSox

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_net")

def main():
    # Use local storage
    import os
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage")
    
    # 1. Create VM with networking enabled
    logger.info("Creating VM from python:alpine with networking...")
    # Note: This requires sudo access for TAP creation
    vm = bs.create_vm("python:alpine", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=True)
    
    try:
        logger.info(f"VM {vm.vm_id} started. Waiting for boot...")
        # Wait for agent
        # We can implement a wait_for_agent method or just sleep/poll
        time.sleep(5)
        
        # 2. Install curl (since python:alpine doesn't have it)
        # This implicitly tests internet access (DNS + HTTP)
        logger.info("Installing curl via apk...")
        def print_output(data):
            logger.info(f"VM: {data.strip()}")
            
        exit_code = vm.exec_command("apk update && apk add curl", on_stdout=print_output, on_stderr=print_output, timeout=60)
        if exit_code != 0:
            logger.error("Failed to install curl. Internet access might be broken.")
            sys.exit(1)
            
        # 3. Verify curl
        logger.info("Verifying internet with curl...")
        exit_code = vm.exec_command("curl -I https://www.google.com", on_stdout=print_output, on_stderr=print_output)
        if exit_code != 0:
            logger.error("Curl failed.")
            sys.exit(1)
            
        logger.info("Internet verification PASSED!")
        vm.stop()
        
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        vm.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
