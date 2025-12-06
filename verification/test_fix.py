import logging
logging.basicConfig(level=logging.INFO)
from bandsox.core import BandSox
import time
import sys

bs = BandSox(storage_dir="/home/rc/bandsox/storage")

print("Creating VM...")
# Use python_alpine which exists
vm1 = bs.create_vm("python_alpine", name="vm1", enable_networking=True)
print(f"VM1 created: {vm1.vm_id}")

# Wait for boot
time.sleep(2)

print("Snapshotting VM1...")
snap_name = bs.snapshot_vm(vm1, "test-snap")
print(f"Snapshot created: {snap_name}")

vm1.stop()

print("Restoring VM from snapshot...")
vm2 = bs.restore_vm(snap_name, enable_networking=True)
print(f"VM2 restored: {vm2.vm_id}")

def print_out(d): print(f"VM2: {d.strip()}")

print("Testing network in VM2...")
exit_code = vm2.exec_command("ping -c 1 8.8.8.8", on_stdout=print_out, on_stderr=print_out)
res = vm2.exec_command("ping -c 1 google.com", on_stdout=print_out, on_stderr=print_out)
input()
vm2.stop()
vm2.delete()
bs.delete_snapshot(snap_name)

if exit_code == 0:
    print("SUCCESS: Network working in restored VM")
    sys.exit(0)
else:
    print("FAILURE: Network not working")
    sys.exit(1)
