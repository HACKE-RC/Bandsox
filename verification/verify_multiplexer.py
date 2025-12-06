import time
import os
import threading
import sys
from bandsox.core import BandSox
from bandsox.vm import MicroVM

def owner_thread(bs, vm_ready_event, stop_event):
    print("[Owner] Creating VM...")
    try:
        vm = bs.create_vm("python:alpine", name="mux-test-vm", vcpu=1, mem_mib=256, enable_networking=False)
        print(f"[Owner] VM {vm.vm_id} created and started.")
        vm_ready_event.set()
        
        while not stop_event.is_set():
            time.sleep(1)
            
        print("[Owner] Stopping VM...")
        vm.stop()
        vm.delete()
    except Exception as e:
        print(f"[Owner] Error: {e}")
        vm_ready_event.set() # Unblock

def client_thread(bs, vm_id, success_event):
    print("[Client] Connecting to VM...")
    # Simulate server connecting to existing VM
    # We use bs.get_vm which returns a ManagedMicroVM connected via socket (since active_vms is empty in this thread/process context if it were separate, but here it shares memory? 
    # Wait, BandSox.active_vms is instance specific.
    # If we use the SAME bs instance, it will return the cached VM.
    # We want to simulate a DIFFERENT process.
    # So we should create a NEW BandSox instance.
    
    cwd = os.getcwd()
    bs2 = BandSox(storage_dir=f"{cwd}/storage")
    
    # Wait a bit for VM to be fully ready
    time.sleep(2)
    
    vm = bs2.get_vm(vm_id)
    if not vm:
        print("[Client] VM not found!")
        return

    print(f"[Client] Got VM instance. Process: {vm.process}")
    # vm.process should be None
    
    print("[Client] Starting PTY session...")
    try:
        # This should trigger connect_to_console
        def on_stdout(data):
            import base64
            decoded = base64.b64decode(data).decode('utf-8')
            print(f"[Client] STDOUT: {decoded!r}")
            if "Hello Multiplexer" in decoded:
                print("[Client] SUCCESS: Received expected output")
                success_event.set()
                
        session_id = vm.start_pty_session("echo Hello Multiplexer", on_stdout=on_stdout)
        print(f"[Client] Session {session_id} started.")
        
        # Wait for output
        time.sleep(5)
        
    except Exception as e:
        print(f"[Client] Error: {e}")

def main():
    cwd = os.getcwd()
    bs = BandSox(storage_dir=f"{cwd}/storage")
    
    vm_ready_event = threading.Event()
    stop_event = threading.Event()
    success_event = threading.Event()
    
    t_owner = threading.Thread(target=owner_thread, args=(bs, vm_ready_event, stop_event))
    t_owner.start()
    
    print("Waiting for VM to start...")
    vm_ready_event.wait()
    
    # Get the VM ID from the owner thread? 
    # We need to find the VM ID.
    # Let's list VMs.
    time.sleep(2)
    vms = bs.list_vms()
    # Find the one we just created
    target_vm = None
    for v in vms:
        if v.get("name") == "mux-test-vm" and v.get("status") == "running":
            target_vm = v
            break
            
    if not target_vm:
        print("Could not find test VM")
        stop_event.set()
        t_owner.join()
        return

    print(f"Target VM: {target_vm['id']}")
    
    t_client = threading.Thread(target=client_thread, args=(bs, target_vm['id'], success_event))
    t_client.start()
    t_client.join()
    
    stop_event.set()
    t_owner.join()
    
    if success_event.is_set():
        print("Verification PASSED")
    else:
        print("Verification FAILED")

if __name__ == "__main__":
    main()
