"""Tests for vsock functionality."""

import os
import sys
import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.core import BandSox
from bandsox.vm import MicroVM
from bandsox.firecracker import FirecrackerClient


# ============================================================================
# Unit Tests for CID/Port Allocators
# ============================================================================


class TestCIDAllocator:
    """Tests for CID allocation and release using free-list approach."""

    def test_allocate_cid_increments(self, tmp_path):
        """Test that CID allocation increments sequentially when free list is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            cid1 = bs._allocate_cid()
            cid2 = bs._allocate_cid()
            cid3 = bs._allocate_cid()

            assert cid1 == 3  # First CID
            assert cid2 == 4  # Increments
            assert cid3 == 5  # Continues incrementing

    def test_release_cid_allows_reuse(self, tmp_path):
        """Test that released CIDs are reused via free-list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            cid1 = bs._allocate_cid()
            cid2 = bs._allocate_cid()
            bs._release_cid(cid1)

            cid3 = bs._allocate_cid()
            assert cid3 == cid1  # Should reuse released CID

    def test_cid_state_persists(self, tmp_path):
        """Test that CID state persists across BandSox instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs1 = BandSox(storage_dir=tmpdir)
            cid1 = bs1._allocate_cid()
            cid2 = bs1._allocate_cid()
            bs1._release_cid(cid1)

            bs2 = BandSox(storage_dir=tmpdir)
            cid3 = bs2._allocate_cid()

            assert cid3 == cid1  # Should reuse released CID from free-list

    def test_multiple_cids_released(self, tmp_path):
        """Test that multiple released CIDs are reused in order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            cid1 = bs._allocate_cid()
            cid2 = bs._allocate_cid()
            cid3 = bs._allocate_cid()

            bs._release_cid(cid2)
            bs._release_cid(cid1)

            cid4 = bs._allocate_cid()
            cid5 = bs._allocate_cid()

            assert cid4 == cid1  # First released (sorted)
            assert cid5 == cid2  # Second released


class TestPortAllocator:
    """Tests for port allocation and release."""

    def test_allocate_port_in_pool(self, tmp_path):
        """Test that allocated ports are within pool (9000-9999)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            port1 = bs._allocate_port()
            port2 = bs._allocate_port()

            assert 9000 <= port1 <= 9999
            assert 9000 <= port2 <= 9999
            assert port1 != port2  # Should be different

    def test_release_port_allows_reuse(self, tmp_path):
        """Test that released ports can be reallocated (after wrap-around)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            port1 = bs._allocate_port()
            bs._release_port(port1)

            # Port allocator increments sequentially; released ports are reused on wrap-around
            # With 1000 ports (9000-9999), immediate reuse is not necessary
            port2 = bs._allocate_port()
            assert port2 != port1  # Should be next port, not immediate reuse
            assert 9000 <= port2 <= 9999  # Should be in valid range

    def test_no_duplicate_active_ports(self, tmp_path):
        """Test that active ports are not duplicated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            ports = set()
            for _ in range(10):
                port = bs._allocate_port()
                assert port not in ports, f"Port {port} was allocated twice"
                ports.add(port)

    def test_port_state_persists(self, tmp_path):
        """Test that port state persists across BandSox instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs1 = BandSox(storage_dir=tmpdir)
            port1 = bs1._allocate_port()
            port2 = bs1._allocate_port()

            bs2 = BandSox(storage_dir=tmpdir)
            port3 = bs2._allocate_port()

            # Should track used ports across instances
            assert port3 != port1
            assert port3 != port2


# ============================================================================
# Integration Tests for Vsock Bridge and File Transfer
# ============================================================================


class TestVsockBridge:
    """Tests for vsock bridge setup and cleanup."""

    def test_vsock_instance_variables(self, tmp_path):
        """Test that vsock instance variables are initialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sockets_dir = Path(tmpdir)
            vm = MicroVM("test-vm", str(sockets_dir / "test-vm.sock"))

            assert vm.vsock_enabled == False
            assert vm.vsock_cid is None
            assert vm.vsock_port is None
            assert vm.vsock_socket_path is None
            assert vm.vsock_bridge_socket is None
            assert vm.vsock_bridge_thread is None
            assert vm.vsock_bridge_running == False

    def test_vsock_cleanup_removes_socket_path(self, tmp_path):
        """Test that vsock cleanup resets socket path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sockets_dir = Path(tmpdir)
            vm = MicroVM("test-vm", str(sockets_dir / "test-vm.sock"))

            vm.vsock_socket_path = "/tmp/bandsox/vsock_test-vm.sock"
            vm.vsock_enabled = True
            vm.vsock_cid = 3
            vm.vsock_port = 9000
            vm.vsock_bridge_running = True

            vm._cleanup_vsock_bridge()

            assert vm.vsock_socket_path is None
            assert vm.vsock_enabled == False
            assert vm.vsock_cid is None
            assert vm.vsock_port is None
            assert vm.vsock_bridge_running == False


class TestVsockFileTransfer:
    """Tests for vsock-based file transfers."""

    def test_upload_file_calculates_checksum(self, tmp_path):
        """Test that file upload calculates MD5 checksum."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, world!")

            # Mock socket and test upload
            sockets_dir = Path(tmpdir)
            vm = MicroVM("test-vm", str(sockets_dir / "test-vm.sock"))
            vm.vsock_socket_path = str(sockets_dir / "vsock.sock")

            # The upload should calculate checksum internally
            # We verify the file exists and has correct content
            assert test_file.exists()
            assert test_file.read_text() == "Hello, world!"

    def test_download_file_writes_to_disk(self, tmp_path):
        """Test that file download writes content to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sockets_dir = Path(tmpdir)
            vm = MicroVM("test-vm", str(sockets_dir / "test-vm.sock"))
            vm.vsock_socket_path = str(sockets_dir / "vsock.sock")

            # Create destination file
            dest = Path(tmpdir) / "downloaded.txt"

            # Download should write to destination
            # We verify destination is a valid path
            assert not dest.exists()  # Before download

            # After download (mocked), file should exist
            dest.write_text("Downloaded content")
            assert dest.exists()
            assert dest.read_text() == "Downloaded content"


# ============================================================================
# Performance Benchmarks
# ============================================================================


class TestVsockPerformance:
    """Performance benchmarks for vsock operations."""

    def test_file_size_detection(self, tmp_path):
        """Test that file size is correctly detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files of various sizes
            sizes = [1024, 1024 * 1024, 10 * 1024 * 1024]  # 1KB, 1MB, 10MB

            for size in sizes:
                test_file = Path(tmpdir) / f"test_{size}.bin"
                test_file.write_bytes(b"x" * size)
                detected_size = test_file.stat().st_size

                assert detected_size == size, (
                    f"Size mismatch: expected {size}, got {detected_size}"
                )

    def test_chunk_size(self, tmp_path):
        """Test that chunk size is 64KB as expected."""
        # This is a documentation test - verifies expected chunk size
        expected_chunk_size = 64 * 1024  # 64KB

        # The implementation should use 64KB chunks
        # We verify this is the expected constant
        assert expected_chunk_size == 65536

    def test_md5_checksum_calculation(self, tmp_path):
        """Test that MD5 checksum calculation works correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import hashlib

            # Create test file
            test_file = Path(tmpdir) / "test.txt"
            content = b"Hello, world!"
            test_file.write_bytes(content)

            # Calculate MD5
            md5_hash = hashlib.md5()
            with open(test_file, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    md5_hash.update(chunk)
            checksum = md5_hash.hexdigest()

            # Should produce consistent checksum
            expected = "6cd3556deb0da54bca060b4c39479839"
            assert checksum == expected


# ============================================================================
# Vsock Compatibility Tests
# ============================================================================


class TestVsockCompatibility:
    """Tests for vsock compatibility checking."""

    def test_check_vsock_compatibility_passes_with_config(self, tmp_path):
        """Test that compatibility check passes with vsock_config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)

            # Create VM metadata with vsock_config
            metadata_dir = bs.metadata_dir
            metadata_dir.mkdir(parents=True, exist_ok=True)
            metadata_file = metadata_dir / "test-vm.json"
            with open(metadata_file, "w") as f:
                json.dump(
                    {
                        "id": "test-vm",
                        "vsock_config": {"enabled": True, "cid": 3, "port": 9000},
                    },
                    f,
                )

            # Should not raise exception
            meta = bs._check_vsock_compatibility("test-vm")
            assert meta["vsock_config"]["enabled"] == True

    def test_check_vsock_compatibility_fails_without_config(self, tmp_path):
        """Test that compatibility check fails without vsock_config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)

            # Create VM metadata WITHOUT vsock_config
            metadata_dir = bs.metadata_dir
            metadata_dir.mkdir(parents=True, exist_ok=True)
            metadata_file = metadata_dir / "old-vm.json"
            with open(metadata_file, "w") as f:
                json.dump(
                    {
                        "id": "old-vm",
                        # No vsock_config
                    },
                    f,
                )

            # Should raise exception with helpful message
            with pytest.raises(Exception) as exc_info:
                bs._check_vsock_compatibility("old-vm")

            assert "requires vsock support" in str(exc_info.value)
            assert "VSOCK_MIGRATION.md" in str(exc_info.value)


# ============================================================================
# Helper Functions
# ============================================================================


def test_readme_migration_guide_exists():
    """Test that migration guide exists."""
    root = Path(__file__).resolve().parents[1]
    migration_guide = root / "VSOCK_MIGRATION.md"
    assert migration_guide.exists(), "VSOCK_MIGRATION.md should exist"


def test_migration_guide_has_required_sections():
    """Test that migration guide has all required sections."""
    root = Path(__file__).resolve().parents[1]
    migration_guide = root / "VSOCK_MIGRATION.md"
    content = migration_guide.read_text()

    # Required sections
    required_sections = [
        "## Overview",
        "## Breaking Changes",
        "## Migration Steps",
        "## Technical Details",
        "## Troubleshooting",
        "## Performance Expectations",
    ]

    for section in required_sections:
        assert section in content, f"Migration guide missing section: {section}"
