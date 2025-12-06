import sys
import time
import logging
import requests
import subprocess
import threading
from bandsox.core import BandSox

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_dashboard")

def main():
    import os
    cwd = os.getcwd()
    
    # 1. Start Server in Background
    logger.info("Starting dashboard server...")
    server_proc = subprocess.Popen(
        [f"{cwd}/.venv/bin/python", "-m", "bandsox.cli", "serve", "--port", "8001"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    try:
        # Wait for server to start
        time.sleep(5)
        
        # 2. Create a VM (so we have something to list)
        bs = BandSox(storage_dir=f"{cwd}/storage")
        logger.info("Creating VM...")
        vm = bs.create_vm("python:alpine", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=False)
        
        try:
            # 3. Query API
            logger.info("Querying API...")
            res = requests.get("http://localhost:8001/api/vms")
            if res.status_code != 200:
                logger.error(f"API failed: {res.status_code} {res.text}")
                sys.exit(1)
                
            vms = res.json()
            logger.info(f"VMs found: {len(vms)}")
            
            found = False
            for v in vms:
                if v["id"] == vm.vm_id:
                    found = True
                    logger.info(f"Found VM {vm.vm_id} in API response")
                    break
            
            if not found:
                logger.error("VM not found in API response")
                sys.exit(1)
                
            logger.info("Dashboard verification PASSED!")
            
        finally:
            vm.stop()
            
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        sys.exit(1)
    finally:
        server_proc.terminate()
        server_proc.wait()

if __name__ == "__main__":
    main()
