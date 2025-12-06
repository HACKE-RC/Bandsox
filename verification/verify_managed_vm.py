
import sys
import os
import time
import logging
import threading

# Add parent dir to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bandsox.core import BandSox

# Configure logging
logging.basicConfig(level=logging.INFO)

def run_vm_background(bs):
    # This simulates the "owner" process
    cwd = os.getcwd()
    vm = bs.create_vm_from_dockerfile(f"{cwd}/docker_context/Dockerfile", tag="alpine-python", name="bg-vm", vcpu=1, mem_mib=512, kernel_path=f"{cwd}/vmlinux")
    print(f"Background VM started: {vm.vm_id}")
    
    # Keep it alive
    time.sleep(10)
    vm.stop()
    print("Background VM stopped")
    return vm.vm_id

def test_managed_vm():
    cwd = os.getcwd()
    # Use valid storage location
    bs = BandSox(storage_dir=f"{cwd}/storage_verify_managed")
    
    # Start VM in a thread to simulate separate ownership (partially)
    # Ideally this would be a separate process, but thread shares memory so bs.active_vms might interfere.
    # But get_vm checks active_vms first.
    # To test the "not owner" case, we must ensure get_vm returns a NEW instance or the existing instance has process=None.
    # BandSox.get_vm returns active_vms[id] if present.
    
    # So we need to create the VM, then remove it from active_vms, then call get_vm.
    vm = bs.create_vm_from_dockerfile(f"{cwd}/docker_context/Dockerfile", tag="alpine-python", name="bg-vm", vcpu=1, mem_mib=512, kernel_path=f"{cwd}/vmlinux")
    vm_id = vm.vm_id
    print(f"VM started: {vm_id}")
    
    # Simulate "server" process which doesn't have the VM in memory yet
    del bs.active_vms[vm_id]
    
    print("Getting ManagedMicroVM (should not have process handle)...")
    managed_vm = bs.get_vm(vm_id)
    
    if managed_vm.process is not None:
        print("TEST INVALID: ManagedMicroVM somehow has process handle!")
        return
        
    print("Attempting start_pty_session (should wait and connect)...")
    try:
        session_id = managed_vm.start_pty_session("/bin/sh")
        print(f"Session started: {session_id}")
        
        # Verify connection exists
        if not managed_vm.console_conn:
            print("FAILED: Session started but console_conn is None!")
        else:
            print("SUCCESS: console_conn established.")
            
    except Exception as e:
        print(f"FAILED: start_pty_session raised {e}")
    finally:
        # Cleanup original VM (we need the handle with process to stop it cleanly, or rely on pid file)
        # managed_vm.stop() uses PID from metadata, so it should work.
        managed_vm.stop()

if __name__ == "__main__":
    test_managed_vm()
