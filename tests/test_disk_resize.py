import logging
import os
import shutil
from pathlib import Path
from bandsox.core import BandSox
from bandsox.image import build_rootfs
import time

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_disk_resize")

def test_disk_resize():
    bs = BandSox()
    
    # 1. Create a base image if not exists (using a small dummy one if possible, but we need build_rootfs)
    # We will use 'python:3.9-alpine' to ensure python3 is present for the agent
    image = "python:3.9-alpine"
    
    # 2. Create VM with custom size (e.g. 100MB, default is usually dynamic or small)
    # The default build_rootfs in image.py creates 4096MB (4GB) by default!
    # Let's try to increase it to 4500MB
    disk_size = 4500
    
    logger.info(f"Creating VM with disk size {disk_size} MiB")
    vm = bs.create_vm(image, name="test-resize", disk_size_mib=disk_size)
    
    try:
        # 3. Check physical file size
        rootfs_path = Path(vm.rootfs_path)
        actual_size = rootfs_path.stat().st_size
        expected_size = disk_size * 1024 * 1024
        
        logger.info(f"Actual size: {actual_size}, Expected: {expected_size}")
        
        # Allow some margin? truncate should be exact if sparse, but file systems might behave differently.
        # But truncate -s is exact.
        if actual_size != expected_size:
            logger.error(f"Size mismatch! Expected {expected_size}, got {actual_size}")
            # Note: If the base image was ALREADY larger than 4500, we skip resize.
            # default is 4096 in image.py, so 4500 should trigger resize.
        else:
            logger.info("Physical file size matches.")
            
        # 4. Boot and check guest FS size
        # We need to wait for agent
        logger.info("Waiting for agent...")
        if vm.wait_for_agent(timeout=20):
            # Run df -m /
            # We expect size around 4400-4500 depending on overhead
            exit_code = vm.send_request("exec", {"command": "df -m /", "background": False}, 
                                        on_stdout=lambda x: logger.info(f"STDOUT: {x}"),
                                        on_stderr=lambda x: logger.error(f"STDERR: {x}"))
            
            # We can parse stdout if we capture it
            output = []
            vm.send_request("exec", {"command": "df -m / | grep '/$'", "background": False},
                            on_stdout=lambda x: output.append(x))
            
            if output:
                line = "".join(output).strip()
                # Filesystem     1M-blocks  Used Available Use% Mounted on
                # /dev/root           4428    ...
                try:
                    parts = line.split()
                    size_in_guest = int(parts[1])
                    logger.info(f"Guest sees size: {size_in_guest} MiB")
                    
                    if size_in_guest > 4200: # 4096 is default, so if > 4200 we resized successfully
                        logger.info("SUCCESS: Guest sees increased disk size.")
                    else:
                        logger.error((f"FAILURE: Guest sees original size? {size_in_guest}"))
                        
                except Exception as e:
                    logger.error(f"Failed to parse df output: {e}")
        else:
            logger.error("Agent failed to start")
            
    finally:
        logger.info("Cleaning up...")
        vm.stop()
        bs.delete_vm(vm.vm_id)

if __name__ == "__main__":
    test_disk_resize()
