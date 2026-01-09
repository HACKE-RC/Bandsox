#!/usr/bin/env python3
"""Test snapshot and restore flow with vsock."""

import sys
import os
import time
from bandsox.core import BandSox


def test_snapshot_restore_flow():
    """Test the full snapshot/restore cycle with vsock."""

    print("=" * 60)
    print("Testing Snapshot/Restore Flow with Vsock")
    print("=" * 60)

    bs = BandSox()

    # Step 1: Create a VM with vsock
    print("\n1. Creating VM with vsock enabled...")
    vm = bs.create_vm(
        "python:3-alpine",
        name="test-snapshot-restore",
        vcpu=1,
        mem_mib=128,
        enable_networking=False,
        enable_vsock=True,
    )

    print(f"   VM ID: {vm.vm_id}")
    print(f"   Vsock enabled: {vm.vsock_enabled}")
    print(f"   Vsock CID: {vm.vsock_cid}")
    print(f"   Vsock port: {vm.vsock_port}")
    print(f"   Vsock socket: {vm.vsock_socket_path}")

    # Step 2: Run some code to ensure agent is ready
    print("\n2. Running Python code to test vsock...")
    result = vm.exec_python_capture("print('Hello from vsock!')")
    print(f"   Output: {result['stdout'].strip()}")
    print(f"   Success: {result['success']}")

    # Step 3: Snapshot the VM
    print("\n3. Taking snapshot...")
    snapshot_name = bs.snapshot_vm(vm, metadata={"test": "vsock_snapshot_restore"})
    print(f"   Snapshot ID: {snapshot_name}")

    # Step 4: Stop the VM
    print("\n4. Stopping VM...")
    vm.stop()
    bs.delete_vm(vm.vm_id)
    print("   VM stopped and deleted")

    # Step 5: Restore from snapshot
    print("\n5. Restoring from snapshot...")
    try:
        restored_vm = bs.restore_vm(
            snapshot_name,
            name="restored-from-snapshot",
            enable_networking=False,
            detach=False,
        )

        print(f"   Restored VM ID: {restored_vm.vm_id}")
        print(f"   Vsock enabled: {restored_vm.vsock_enabled}")
        print(f"   Vsock CID: {restored_vm.vsock_cid}")
        print(f"   Vsock port: {restored_vm.vsock_port}")
        print(f"   Vsock socket: {restored_vm.vsock_socket_path}")

        # Step 6: Test vsock in restored VM
        print("\n6. Testing vsock in restored VM...")
        result = restored_vm.exec_python_capture("print('Hello from restored vsock!')")
        print(f"   Output: {result['stdout'].strip()}")
        print(f"   Success: {result['success']}")

        # Cleanup
        print("\n7. Cleaning up...")
        restored_vm.stop()
        bs.delete_vm(restored_vm.vm_id)
        bs.delete_snapshot(snapshot_name)
        print("   Cleanup complete")

        print("\n" + "=" * 60)
        print("TEST PASSED: Snapshot/Restore with vsock works!")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"\n\nTEST FAILED: {e}")
        import traceback

        traceback.print_exc()

        # Cleanup on failure
        try:
            if "restored_vm" in locals():
                restored_vm.stop()
                bs.delete_vm(restored_vm.vm_id)
            bs.delete_snapshot(snapshot_name)
        except:
            pass

        print("\n" + "=" * 60)
        print("TEST FAILED")
        print("=" * 60)
        return False


if __name__ == "__main__":
    print("Vsock Snapshot/Restore Test")
    print("=" * 60)
    print()
    print("This test requires sudo to run.")
    print("Run with: sudo python3 test_snapshot_restore_flow.py")
    print()
    print("The test will:")
    print("  1. Create VM with vsock")
    print("  2. Snapshot the VM")
    print("  3. Stop and delete VM")
    print("  4. Restore from snapshot")
    print("  5. Test vsock in restored VM")
    print("=" * 60)

    if not os.environ.get("SUDO_UID"):
        print("WARNING: Not running as sudo. Tests may fail.")
        print()

    sys.exit(0 if test_snapshot_restore_flow() else 1)
