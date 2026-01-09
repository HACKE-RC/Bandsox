# Vsock Migration Guide

## Overview

BandSox has switched from debugfs to vsock-based file transfers, providing 100-10000x performance improvement with no VM pause requirement. This is a breaking change that requires recreating existing VMs.

## Breaking Changes

### Old VMs Will Not Start

VMs created before this update **will not start** because they lack vsock configuration metadata. You will see an error like:

```
VM requires vsock support. Please recreate the VM to enable vsock.
```

### Why This Change?

- **Performance**: File transfers are 100-10000x faster (10-100 MB/s vs KB/s for large files)
- **No VM Pause**: No need to pause the VM for file operations
- **Stability**: Vsock is the official Firecracker communication channel
- **No Debugfs**: Removes dependency on debugfs mount

## Migration Steps

### Step 1: Back Up Your Data (If Needed)

If you have important files inside an old VM that you want to preserve:

```bash
# Start the old VM in serial-only mode
sudo python3 -m bandsox.cli start <old_vm_id>

# Download files using the agent (slow, but works)
sudo python3 -m bandsox.cli download <old_vm_id> /path/in/vm /local/path

# Stop the VM
sudo python3 -m bandsox.cli stop <old_vm_id>
```

### Step 2: Remove Old VMs

```bash
# List VMs
sudo python3 -m bandsox.cli list

# Delete old VMs (this only removes metadata, not images)
sudo python3 -m bandsox.cli rm <old_vm_id>
```

### Step 3: Create New VMs

```bash
# Create a new VM with vsock support (automatic)
sudo python3 -m bandsox.cli create ubuntu:latest --name my-new-vm

# Or with a custom image
sudo python3 -m bandsox.cli create /path/to/rootfs.ext4 --name my-new-vm
```

New VMs will automatically have vsock enabled with:
- Unique CID (Context ID) allocated from pool (3-254)
- Unique port allocated from pool (9000-9999)
- Vsock bridge configured automatically

### Step 4: Verify Vsock is Working

```bash
# Start the VM
sudo python3 -m bandsox.cli start my-new-vm

# Upload a file (should be much faster)
echo "Hello, world!" > test.txt
sudo python3 -m bandsox.cli upload my-new-vm test.txt /tmp/test.txt

# Download the file
sudo python3 -m bandsox.cli download my-new-vm /tmp/test.txt downloaded.txt

# Verify content
cat downloaded.txt
```

Check the logs for vsock-related messages:

```bash
# Should see messages like:
# "Vsock enabled: CID=3, Port=9000"
# "File uploaded via vsock: /tmp/test.txt (13 bytes)"
```

## Technical Details

### Vsock Architecture

```
Host (BandSox)                          Guest (Agent)
    |                                        |
    |-- Unix Socket --> Vsock Bridge         |
    |                   |                    |
    |                AF_VSOCK              |
    |                   |                    |
    |                Port 9000               |
    |                   |                    |
    |---------------> Guest CID=3 -----------|
                            |
                            v
                   Guest Agent (ttyS0)
```

### CID Allocation

- CID 0: Hypervisor (reserved)
- CID 1: Local (reserved)
- CID 2: Host (reserved)
- CIDs 3-254: Guest VMs (allocated sequentially)
- State stored in: `/var/lib/bandsox/cid_allocator.json`

### Port Allocation

- Port pool: 9000-9999 (1000 ports available)
- Dynamic allocation with collision tracking
- Supports port reuse after release
- State stored in: `/var/lib/bandsox/port_allocator.json`

### File Transfer Protocol

**Upload:**
1. Host sends `{"type": "upload", "path": "...", "size": ..., "checksum": "..."}`
2. Guest responds with `{"type": "ready", ...}`
3. Host sends base64-encoded chunks (64KB each)
4. Guest acknowledges each chunk with `{"type": "ack", "bytes": ...}`
5. Host sends `{"type": "verify"}`
6. Guest verifies MD5 checksum
7. Guest responds with `{"type": "complete", "size": ...}`

**Download:**
1. Host sends `{"type": "download", "path": "..."}`
2. Guest sends chunks with `{"type": "chunk", "data": "...", "size": ..., "offset": ...}`
3. Guest responds with `{"type": "complete", "size": ...}`

### Fallback Behavior

The guest agent supports dual-mode operation:
- **Primary**: Vsock (if kernel module `virtio-vsock` is available)
- **Fallback**: Serial (if vsock connection fails)

This ensures compatibility with custom kernels that might not have vsock support.

## Troubleshooting

### "VM requires vsock support" Error

This means the VM metadata doesn't contain vsock configuration. You must recreate the VM.

### "Vsock connection failed" Warning

The guest kernel might not have vsock module support. Check:

```bash
# In the guest VM
lsmod | grep vsock
# Should see: virtio_vsock

# If not available, check kernel config
zcat /proc/config.gz | grep VSOCK
# Should see: CONFIG_VIRTIO_VSOCK=y or m
```

### Slow File Transfers

If transfers are slow, check if vsock is being used:

```bash
# Look for these log messages:
# "File uploaded via vsock: ..." (fast)
# "File uploaded via agent: ..." (slow serial fallback)
```

### CID/Port Exhaustion

If you see "No available CIDs" or "No available ports":

```bash
# Check allocator state
cat /var/lib/bandsox/cid_allocator.json
cat /var/lib/bandsox/port_allocator.json

# If needed, reset allocators (WARNING: Only do this if no VMs are running)
echo '{"next_cid": 3}' > /var/lib/bandsox/cid_allocator.json
echo '{"next_port": 9000, "used_ports": []}' > /var/lib/bandsox/port_allocator.json
```

## Performance Expectations

Expected file transfer speeds (typical hardware):

| File Size | Debugfs (Old) | Vsock (New) | Improvement |
|-----------|---------------|-------------|-------------|
| 1 MB      | ~0.1 MB/s     | ~50 MB/s    | 500x        |
| 10 MB     | ~0.05 MB/s    | ~80 MB/s    | 1600x       |
| 100 MB    | ~0.02 MB/s    | ~100 MB/s   | 5000x       |
| 1 GB      | ~0.01 MB/s    | ~100 MB/s   | 10000x      |

*Actual performance depends on system configuration and workloads*

## Rolling Back

If you need to use an old VM temporarily, you can manually add vsock metadata:

```bash
# Edit VM metadata file
nano /var/lib/bandsox/metadata/<vm_id>.json

# Add this line to the JSON:
# "vsock_config": {"enabled": false}

# This allows the VM to start without vsock (uses serial only)
```

However, this is **not recommended** as it disables the performance benefits.

## Next Steps

After migrating to vsock:

1. **Test thoroughly**: Verify file operations work as expected
2. **Monitor performance**: You should see significant speedups
3. **Clean up**: Remove old VMs once you're satisfied with the new ones
4. **Report issues**: If you encounter problems, please report them

## Support

For questions or issues:
- Check logs: `journalctl -u bandsox` (if running as service)
- Verbose mode: `sudo python3 -m bandsox.cli start <vm_id> --verbose`
- Report bugs: https://github.com/anomalyco/bandsox/issues

---

**Last updated:** January 2026
**BandSox version:** 2.0.0+
