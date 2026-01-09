#!/bin/bash
# cleanup_vsock.sh - Cleanup stale BandSox resources

set -e

echo "=== BandSox Cleanup Script ==="
echo ""

# Kill all firecracker processes
echo "Killing firecracker processes..."
sudo pkill -9 -f "firecracker" 2>/dev/null || true
sleep 1

# Remove all vsock socket files
echo "Removing vsock socket files..."
sudo rm -f /tmp/bandsox/vsock_*.sock 2>/dev/null || true

# Remove stale socket files
echo "Removing stale socket files..."
sudo rm -f /var/lib/bandsox/sockets/*.sock 2>/dev/null || true

# Reset CID allocator
echo "Resetting CID allocator..."
echo '{"free_cids": [], "next_cid": 3}' | sudo tee /var/lib/bandsox/cid_allocator.json > /dev/null

# Reset port allocator
echo "Resetting port allocator..."
echo '{"next_port": 9000, "used_ports": []}' | sudo tee /var/lib/bandsox/port_allocator.json > /dev/null

echo ""
echo "=== Cleanup Complete ==="
echo ""

# Show current state
echo "Current state:"
echo "  CID allocator: $(cat /var/lib/bandsox/cid_allocator.json)"
echo "  Port allocator: $(cat /var/lib/bandsox/port_allocator.json)"
echo "  Vsock sockets: $(ls /tmp/bandsox/ 2>/dev/null | wc -l)"
echo "  Sockets dir: $(ls /var/lib/bandsox/sockets/ 2>/dev/null | wc -l) files"
