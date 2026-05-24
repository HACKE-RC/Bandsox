import sys
import time
import logging
from bandsox.core import BandSox

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify")

def main():
    # Use local storage to avoid sudo issues
    import os
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage")
    
    # 1. Create VM
    logger.info("Creating VM from python:alpine...")
    # We use python:alpine to ensure python3 is available for the agent.
    vm = bs.create_vm("python:alpine", name="test-python-vm", vcpu=1, mem_mib=256, kernel_path=f"{cwd}/vmlinux", enable_networking=False)
    
    try:
        logger.info(f"VM {vm.vm_id} started. Waiting for boot...")
        time.sleep(5)
        
        # 2. Verify Internet Access (Manual check via logs or if we had a way to exec)
        # Since we don't have a way to exec command inside easily without ssh/agent,
        # we rely on the fact that if it boots and doesn't crash, it's good.
        # Ideally we would have an agent inside.
        # For this demo, we just check if it stays running.
        
        if vm.process.poll() is not None:
            logger.error("VM crashed!")
            out, err = vm.process.communicate()
            logger.error(f"STDOUT: {out}")
            logger.error(f"STDERR: {err}")
            sys.exit(1)
            
        logger.info("VM is running.")
        
        # 2.5 Verify Command Execution
        logger.info("Verifying command execution...")
        def print_stdout(data):
            logger.info(f"STDOUT: {data.strip()}")
        def print_stderr(data):
            logger.info(f"STDERR: {data.strip()}")
            
        exit_code = vm.exec_command("echo 'Hello BandSox' > /root/test.txt", on_stdout=print_stdout, on_stderr=print_stderr)
        if exit_code != 0:
            logger.error(f"Command failed with code {exit_code}")
            sys.exit(1)
            
        # Verify file exists
        exit_code = vm.exec_command("cat /root/test.txt", on_stdout=print_stdout, on_stderr=print_stderr)
        if exit_code != 0:
             logger.error("Failed to read file")
             sys.exit(1)

        # 3. Snapshot
        logger.info("Snapshotting VM...")
        snap_id = bs.snapshot_vm(vm, "test_snap")
        logger.info(f"Snapshot created: {snap_id}")
        
        # 4. Kill
        logger.info("Stopping VM...")
        vm.stop()
        
        # 5. Restore
        logger.info("Restoring VM...")
        vm2 = bs.restore_vm(snap_id, enable_networking=False)
        logger.info(f"Restored VM {vm2.vm_id}")
        
        # Wait for agent ready (handled by exec_command internally or we wait)
        # But restore_vm calls resume(), so agent should be running.
        # However, the agent might need to reconnect or just continue reading stdin.
        # Since we start a NEW process for restore, we have new pipes.
        # The agent inside the VM is the SAME process (frozen/thawed).
        # It was reading from ttyS0 (fd 0).
        # When we restore, Firecracker re-attaches ttyS0 to the new process's stdin/stdout?
        # Yes, Firecracker should handle this.
        
        time.sleep(2)
        if vm2.process.poll() is not None:
            logger.error("Restored VM crashed!")
            sys.exit(1)
            
        logger.info("Restored VM is running.")
        
        # 6. Verify Persistence
        logger.info("Verifying file persistence...")
        exit_code = vm2.exec_command("cat /root/test.txt", on_stdout=print_stdout, on_stderr=print_stderr)
        if exit_code != 0:
             logger.error("File persistence check failed!")
             sys.exit(1)
             
        logger.info("Persistence verified!")
        input("VM Running")
        vm2.pause()
        input("VM Paused")
        vm2.stop()
        vm.delete()
        
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        vm.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
