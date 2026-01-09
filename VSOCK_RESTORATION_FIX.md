# Vsock Restoration Fix

## Problem
Restoring VMs from snapshots with vsock enabled was failing with:
```
Firecracker API error: {"fault_message":"Load snapshot error: Failed to restore from snapshot: Failed to build microVM from snapshot: Failed to restore devices: Error restoring MMIO devices: VsockUnixBackend: Error binding to host-side Unix socket: Address in use (os error 98)"}
```

## Root Cause
1. Snapshots save vsock configuration pointing to `/tmp/bandsox/vsock_{old_vm_id}.sock`
2. Firecracker loads snapshot and tries to bind to OLD socket path
3. Old socket file from previous VM still exists (stale)
4. "Address in use" error occurs

**Critical Insight**: Firecracker's binary snapshot file saves vsock device state, not just metadata. Even if we don't save `vsock_config` to `metadata.json`, Firecracker's state file still contains vsock device information. This means:
- Snapshot from vsock-enabled VM → New snapshot inherits vsock state
- Restoring from new snapshot → Firecracker tries to restore vsock device
- Socket file doesn't exist → "Address in use" error

## Solution

### Changes Made

#### 1. `bandsox/core.py` - Disconnect vsock bridge before snapshot (FIX)

Added vsock bridge disconnection in `snapshot_vm()` before taking snapshot:
```python
# Disconnect vsock bridge before snapshot to properly release socket
had_vsock = vm.vsock_enabled and vm.vsock_bridge_running
if had_vsock:
    logger.info(f"Disconnecting vsock bridge before snapshot for {vm.vm_id}")
    vm._cleanup_vsock_bridge()
    # Don't reconnect after snapshot - let guest re-establish vsock connections
    vm.vsock_enabled = False
```

**Why this fixes the issue**:
- Vsock bridge holds the Unix socket file open
- Firecracker can't bind to the socket while bridge has it
- Disconnecting releases the socket, allowing Firecracker to save clean state
- Guest agent will re-establish vsock connections after resume (dual-mode design)

#### 2. `bandsox/core.py` - Import threading
```python
import threading  # Added for vsock bridge thread
```

#### 2. `bandsox/core.py` - Socket cleanup BEFORE loading snapshot (around line 530)
Added cleanup of stale vsock socket before Firecracker loads snapshot:
```python
# Handle vsock socket cleanup before loading snapshot
vsock_config = snapshot_meta.get("vsock_config")
if vsock_config and vsock_config.get("enabled"):
    old_vm_id = snapshot_meta.get("source_vm_id")
    if old_vm_id:
        old_socket_path = f"/tmp/bandsox/vsock_{old_vm_id}.sock"

        # Clean up stale vsock socket to avoid "Address in use" error
        if os.path.exists(old_socket_path):
            try:
                os.unlink(old_socket_path)
                logger.debug(f"Removed stale vsock socket: {old_socket_path}")
            except Exception as e:
                logger.warning(f"Failed to remove vsock socket {old_socket_path}: {e}")

        # Tell VM to expect the old socket path (Firecracker will recreate it)
        vm.vsock_socket_path = old_socket_path
```

#### 3. `bandsox/core.py` - Reconnect vsock bridge AFTER restore (around line 642)
Added reconnection to vsock socket after VM is resumed:
```python
# Re-establish vsock bridge if vsock was enabled in the snapshot
vsock_config = snapshot_meta.get("vsock_config")
if vsock_config and vsock_config.get("enabled"):
    old_vm_id = snapshot_meta.get("source_vm_id", new_vm_id)
    old_socket_path = f"/tmp/bandsox/vsock_{old_vm_id}.sock"

    try:
        # Firecracker should have recreated the socket after loading snapshot
        max_wait = 50
        for i in range(max_wait):
            if os.path.exists(old_socket_path):
                break
            time.sleep(0.1)
        else:
            raise Exception(f"Vsock socket not created after restore: {old_socket_path}")

        # Reconnect to vsock socket
        vm.vsock_bridge_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        vm.vsock_bridge_socket.connect(old_socket_path)
        vm.vsock_bridge_socket.settimeout(30)

        # Use SAME CID/port from snapshot (agent already has them)
        vm.vsock_cid = vsock_config["cid"]
        vm.vsock_port = vsock_config["port"]
        vm.vsock_enabled = True
        vm.vsock_bridge_running = True

        # Restart vsock bridge thread
        vm.vsock_bridge_thread = threading.Thread(
            target=vm._vsock_bridge_loop, daemon=True
        )
        vm.vsock_bridge_thread.start()

        vm.env_vars["BANDSOX_VSOCK_PORT"] = str(vsock_config["port"])
        logger.info(f"Vsock restored: CID={vsock_config['cid']}, port={vsock_config['port']}")
    except Exception as e:
        logger.warning(f"Failed to restore vsock bridge: {e}")
        # VM continues without vsock (falls back to serial)
        vm.vsock_enabled = False
```

#### 4. `bandsox/core.py` - Preserve vsock_config in metadata (around line 743)
Added vsock_config to restored VM metadata:
```python
"vsock_config": vsock_config,  # Preserve vsock_config from snapshot
```

## Design Decisions

### CID/Port Allocation
**Decision**: Use SAME CID/port from snapshot
**Reason**: Firecracker saves guest vsock state (including CID) in the snapshot. The agent inside the VM has this CID baked into its runtime state. Changing CID would require restarting the guest agent, which isn't possible when restoring from snapshot.

### Legacy Snapshots
**Decision**: Disable vsock for snapshots without `vsock_config`
**Reason**: Simplest approach. Snapshots created before vsock implementation don't have `vsock_config` in their metadata, so they restore without vsock and fall back to serial communication.

### Socket Cleanup
**Decision**: Cleanup at restore only (targeted cleanup)
**Reason**: Only remove the specific socket when restoring its snapshot. This is minimal and targeted, avoiding the risk of removing active VM sockets.

## Testing

### Test Suite
A comprehensive test script is provided: `test_vsock_restore.py`

To run tests:
```bash
sudo python3 test_vsock_restore.py
```

### Manual Testing

#### Test 1: Restore with vsock (snapshot has vsock_config)
```bash
# Restore snapshot with vsock enabled
sudo python3 -m bandsox.cli restore ffmpeg-1_init --name test-restore

# Upload a file via vsock (should be fast)
echo "Hello world" > test.txt
sudo python3 -m bandsox.cli upload test-restore test.txt /tmp/test.txt

# Verify it worked
sudo python3 -m bandsox.cli exec test-restore "cat /tmp/test.txt"

# Cleanup
sudo python3 -m bandsox.cli stop test-restore
sudo python3 -m bandsox.cli delete test-restore
```

#### Test 2: Restore without vsock (legacy snapshot)
```bash
# Restore legacy snapshot (no vsock_config)
sudo python3 -m bandsox.cli restore init-box --name test-legacy

# Upload a file via serial (should still work, just slower)
echo "Hello world" > test.txt
sudo python3 -m bandsox.cli upload test-legacy test.txt /tmp/test.txt

# Verify it worked
sudo python3 -m bandsox.cli exec test-legacy "cat /tmp/test.txt"

# Cleanup
sudo python3 -m bandsox.cli stop test-legacy
sudo python3 -m bandsox.cli delete test-legacy
```

#### Test 3: Verify socket cleanup
```bash
# Check for stale sockets before restore
ls -la /tmp/bandsox/

# Restore VM
sudo python3 -m bandsox.cli restore ffmpeg-1_init --name test-cleanup

# Verify old socket was cleaned up
ls -la /tmp/bandsox/

# Check no "Address in use" errors occurred
# (Should be clean in logs)

# Cleanup
sudo python3 -m bandsox.cli stop test-cleanup
sudo python3 -m bandsox.cli delete test-cleanup
```

## Expected Results

### Successful Restore
- VM restores without "Address in use" error
- Vsock is reconnected automatically
- File transfers work at vsock speeds (100-10,000x faster than serial)
- Metadata shows vsock_config preserved

### Legacy Snapshot Restore
- VM restores successfully
- Vsock is disabled (no vsock_config in metadata)
- File transfers work via serial (slower but functional)
- No errors or crashes

### Socket Cleanup
- Stale vsock sockets are removed before restore
- No "Address in use" errors
- `/tmp/bandsox/` remains clean

## Files Modified

| File | Changes | Lines Added/Modified |
|------|----------|-------------------|
| `bandsox/core.py` | Add vsock restoration logic | +55 |
| `bandsox/core.py` | Import threading | +1 |
| `test_vsock_restore.py` | New test script | +250 |

## Verification Steps

1. **Code compiles**: ✅ Verified with `python3 -m py_compile`
2. **Import works**: ✅ Verified with `from bandsox.core import BandSox`
3. **Test script created**: ✅ `test_vsock_restore.py` ready to run
4. **Documentation complete**: ✅ This document explains changes and testing

## Next Steps

1. Run the test script with sudo: `sudo python3 test_vsock_restore.py`
2. Verify all tests pass
3. Manually test restoration with existing snapshots
4. Commit changes with descriptive message

## Rollback Plan

If issues arise, revert to before vsock restoration changes:
```bash
git diff bandsox/core.py  # Review changes
git checkout bandsox/core.py  # Revert if needed
```

Key changes are isolated to `restore_vm()` method in `bandsox/core.py`, making rollback straightforward.
