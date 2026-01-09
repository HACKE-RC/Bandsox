#!/usr/bin/env python3
"""Test script for vsock restoration fix."""

import sys
import os
import json
import time
from pathlib import Path


def test_restore_with_vsock():
    """Test restoring a snapshot with vsock_config."""
    print("=" * 60)
    print("Test 1: Restore snapshot WITH vsock_config")
    print("=" * 60)

    from bandsox.core import BandSox

    bs = BandSox()

    # Check snapshot metadata
    snap_meta_file = Path("/var/lib/bandsox/snapshots/ffmpeg-1_init/metadata.json")
    with open(snap_meta_file) as f:
        snap_meta = json.load(f)

    print(f"Snapshot metadata: {json.dumps(snap_meta, indent=2)}")
    print(f"\nHas vsock_config: {'vsock_config' in snap_meta}")

    if "vsock_config" in snap_meta:
        print(f"Vsock config: {snap_meta['vsock_config']}")
        old_vm_id = snap_meta["source_vm_id"]
        old_socket = f"/tmp/bandsox/vsock_{old_vm_id}.sock"
        print(f"Expected vsock socket: {old_socket}")
        print(f"Socket exists: {os.path.exists(old_socket)}")

    try:
        print("\nStarting restore...")
        vm = bs.restore_vm("ffmpeg-1_init", name="test-restore-vsock", detach=False)

        print(f"Restore successful!")
        print(f"VM ID: {vm.vm_id}")
        print(f"Vsock enabled: {vm.vsock_enabled}")
        print(f"Vsock CID: {vm.vsock_cid}")
        print(f"Vsock port: {vm.vsock_port}")
        print(f"Vsock socket path: {vm.vsock_socket_path}")

        # Check if vsock socket exists
        if vm.vsock_socket_path:
            print(f"Vsock socket exists: {os.path.exists(vm.vsock_socket_path)}")

        # Test vsock file transfer
        print("\nTesting vsock file transfer...")
        test_file = "/tmp/bandsox_test.txt"
        with open(test_file, "w") as f:
            f.write("Hello from vsock test! " * 1000)

        start = time.time()
        try:
            vm.upload_file(test_file, "/tmp/uploaded.txt")
            elapsed = time.time() - start
            print(
                f"Upload completed in {elapsed:.3f}s (via vsock: fast if < 0.1s, serial: slow if > 1s)"
            )

            # Verify file was uploaded
            result = vm.exec_command("cat /tmp/uploaded.txt | wc -c")
            print(f"File size on guest: {result} bytes")

        finally:
            os.unlink(test_file)

        # Cleanup
        print("\nCleaning up...")
        vm.stop()
        bs.delete_vm("test-restore-vsock")
        print("Test passed!")

        return True

    except Exception as e:
        print(f"\nTest FAILED with error: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_restore_without_vsock():
    """Test restoring a snapshot without vsock_config (legacy)."""
    print("\n" + "=" * 60)
    print("Test 2: Restore snapshot WITHOUT vsock_config (legacy)")
    print("=" * 60)

    from bandsox.core import BandSox

    bs = BandSox()

    # Check snapshot metadata
    snap_meta_file = Path("/var/lib/bandsox/snapshots/init-box/metadata.json")
    with open(snap_meta_file) as f:
        snap_meta = json.load(f)

    print(f"Snapshot metadata keys: {list(snap_meta.keys())}")
    print(f"Has vsock_config: {'vsock_config' in snap_meta}")

    try:
        print("\nStarting restore...")
        vm = bs.restore_vm("init-box", name="test-restore-novsock", detach=False)

        print(f"Restore successful!")
        print(f"VM ID: {vm.vm_id}")
        print(f"Vsock enabled: {vm.vsock_enabled}")

        # Test file transfer via serial (should still work, just slower)
        print("\nTesting file transfer via serial...")
        test_file = "/tmp/bandsox_test2.txt"
        with open(test_file, "w") as f:
            f.write("Hello from serial test!")

        start = time.time()
        try:
            vm.upload_file(test_file, "/tmp/uploaded2.txt")
            elapsed = time.time() - start
            print(f"Upload completed in {elapsed:.3f}s (serial: slow, > 1s)")

        finally:
            os.unlink(test_file)

        # Cleanup
        print("\nCleaning up...")
        vm.stop()
        bs.delete_vm("test-restore-novsock")
        print("Test passed!")

        return True

    except Exception as e:
        print(f"\nTest FAILED with error: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("Vsock Restoration Fix Test Suite")
    print("=" * 60)

    # Note: These tests require sudo to work
    # Run with: sudo python3 test_vsock_restore.py

    results = []

    if not os.environ.get("SUDO_UID"):
        print("WARNING: This test requires sudo to run properly.")
        print("Please run with: sudo python3 test_vsock_restore.py")
        print()

    # Run tests
    results.append(("Restore WITH vsock", test_restore_with_vsock()))
    results.append(("Restore WITHOUT vsock", test_restore_without_vsock()))

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    for name, passed in results:
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"{name}: {status}")

    all_passed = all(r[1] for r in results)
    sys.exit(0 if all_passed else 1)
