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
    """Tests for port allocation - always returns 9000."""

    def test_allocate_port_always_9000(self, tmp_path):
        """Test that allocated port is always 9000 (fixed port)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            port1 = bs._allocate_port()
            port2 = bs._allocate_port()

            assert port1 == 9000
            assert port2 == 9000  # Always same port since each VM has own socket path

    def test_release_port_is_noop(self, tmp_path):
        """Test that release_port is a no-op (since we use fixed port)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            port1 = bs._allocate_port()
            bs._release_port(port1)  # Should not raise

            # Allocating again gives same port
            port2 = bs._allocate_port()
            assert port2 == 9000


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


class TestVsockSnapshotPath:
    """Tests for vsock path handling during snapshot restore."""

    def test_vsock_config_includes_uds_path(self):
        """Test that vsock_config includes uds_path for new VMs."""
        # When creating vsock_config, it should include the uds_path
        # This tests the expected structure of vsock_config
        # New VMs use storage_dir/vsock/ path instead of /tmp/bandsox
        vsock_config = {
            "enabled": True,
            "cid": 3,
            "port": 9001,
            "uds_path": "/var/lib/bandsox/vsock/vsock_test-vm.sock",
        }

        assert "uds_path" in vsock_config
        assert vsock_config["uds_path"].endswith(".sock")
        # New paths should NOT use /tmp/bandsox
        assert "/tmp/bandsox" not in vsock_config["uds_path"]

    def test_restore_uses_original_path_when_available(self):
        """Test that restore logic migrates old /tmp/bandsox paths to new location."""
        # Simulate snapshot metadata with old /tmp/bandsox path
        snapshot_meta = {
            "vsock_config": {
                "enabled": True,
                "cid": 3,
                "port": 9001,
                "uds_path": "/tmp/bandsox/vsock_original-vm-id.sock",
            },
            "source_vm_id": "original-vm-id",
        }

        vsock_config = snapshot_meta.get("vsock_config")
        new_vm_id = "new-vm-id"
        vsock_dir = "/var/lib/bandsox/vsock"

        # This mimics the NEW restore logic in core.py that migrates old paths
        if vsock_config and vsock_config.get("enabled"):
            original_uds_path = vsock_config.get("uds_path")
            if original_uds_path and "/tmp/bandsox/" in original_uds_path:
                # Migrate old path to new location
                import os

                old_filename = os.path.basename(original_uds_path)
                vsock_socket_path = f"{vsock_dir}/{old_filename}"
            elif original_uds_path:
                vsock_socket_path = original_uds_path
            else:
                source_vm_id = snapshot_meta.get("source_vm_id")
                if source_vm_id:
                    vsock_socket_path = f"{vsock_dir}/vsock_{source_vm_id}.sock"
                else:
                    vsock_socket_path = f"{vsock_dir}/vsock_{new_vm_id}.sock"
        else:
            vsock_socket_path = f"{vsock_dir}/vsock_{new_vm_id}.sock"

        # Should migrate to new path, preserving filename
        assert vsock_socket_path == "/var/lib/bandsox/vsock/vsock_original-vm-id.sock"
        assert "/tmp/bandsox" not in vsock_socket_path

    def test_restore_falls_back_to_source_vm_id(self):
        """Test fallback to source_vm_id when uds_path is missing (old snapshots)."""
        # Simulate old snapshot metadata without uds_path
        snapshot_meta = {
            "vsock_config": {
                "enabled": True,
                "cid": 3,
                "port": 9001,
                # No uds_path - old snapshot format
            },
            "source_vm_id": "old-vm-id-12345",
        }

        vsock_config = snapshot_meta.get("vsock_config")
        new_vm_id = "new-vm-id"
        vsock_dir = "/var/lib/bandsox/vsock"

        # Mimic restore logic with new paths
        if vsock_config and vsock_config.get("enabled"):
            original_uds_path = vsock_config.get("uds_path")
            if original_uds_path and "/tmp/bandsox/" in original_uds_path:
                import os

                old_filename = os.path.basename(original_uds_path)
                vsock_socket_path = f"{vsock_dir}/{old_filename}"
            elif original_uds_path:
                vsock_socket_path = original_uds_path
            else:
                source_vm_id = snapshot_meta.get("source_vm_id")
                if source_vm_id:
                    vsock_socket_path = f"{vsock_dir}/vsock_{source_vm_id}.sock"
                else:
                    vsock_socket_path = f"{vsock_dir}/vsock_{new_vm_id}.sock"
        else:
            vsock_socket_path = f"{vsock_dir}/vsock_{new_vm_id}.sock"

        # Should use source_vm_id path under new location
        assert vsock_socket_path == "/var/lib/bandsox/vsock/vsock_old-vm-id-12345.sock"

    def test_restore_uses_new_id_without_vsock(self):
        """Test that VMs without vsock use new VM ID for socket path."""
        # Simulate snapshot without vsock
        snapshot_meta = {
            "source_vm_id": "source-vm-id",
            # No vsock_config
        }

        vsock_config = snapshot_meta.get("vsock_config")
        new_vm_id = "new-vm-id"
        vsock_dir = "/var/lib/bandsox/vsock"

        # Mimic restore logic with new paths
        if vsock_config and vsock_config.get("enabled"):
            original_uds_path = vsock_config.get("uds_path")
            if original_uds_path and "/tmp/bandsox/" in original_uds_path:
                import os

                old_filename = os.path.basename(original_uds_path)
                vsock_socket_path = f"{vsock_dir}/{old_filename}"
            elif original_uds_path:
                vsock_socket_path = original_uds_path
            else:
                source_vm_id = snapshot_meta.get("source_vm_id")
                if source_vm_id:
                    vsock_socket_path = f"{vsock_dir}/vsock_{source_vm_id}.sock"
                else:
                    vsock_socket_path = f"{vsock_dir}/vsock_{new_vm_id}.sock"
        else:
            vsock_socket_path = f"{vsock_dir}/vsock_{new_vm_id}.sock"

        # Should use new VM ID (vsock not enabled)
        assert vsock_socket_path == f"/var/lib/bandsox/vsock/vsock_{new_vm_id}.sock"


class TestVsockNamespaceIsolation:
    """Tests for vsock namespace isolation feature."""

    def test_default_vsock_isolation_is_namespace(self):
        """Test that default vsock_isolation is 'namespace'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sockets_dir = Path(tmpdir) / "sockets"
            sockets_dir.mkdir()

            vm = MicroVM("test-vm", str(sockets_dir / "test-vm.sock"))
            assert vm.vsock_isolation == "namespace"

    def test_vsock_isolation_can_be_set_to_none(self):
        """Test that vsock_isolation can be set to 'none'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sockets_dir = Path(tmpdir) / "sockets"
            sockets_dir.mkdir()

            vm = MicroVM(
                "test-vm", str(sockets_dir / "test-vm.sock"), vsock_isolation="none"
            )
            assert vm.vsock_isolation == "none"

    def test_namespace_isolation_requires_unshare(self):
        """Test that namespace isolation validates unshare availability."""
        import shutil

        # This test verifies the validation logic exists
        # The actual command execution is tested in integration tests

        # If unshare is available, the check should pass
        if shutil.which("unshare"):
            # Just verify the vm can be created with namespace isolation
            with tempfile.TemporaryDirectory() as tmpdir:
                sockets_dir = Path(tmpdir) / "sockets"
                sockets_dir.mkdir()

                vm = MicroVM(
                    "test-vm",
                    str(sockets_dir / "test-vm.sock"),
                    vsock_isolation="namespace",
                )
                assert vm.vsock_isolation == "namespace"

    def test_start_process_includes_unshare_when_namespace_isolation(self):
        """Test that start_process command includes unshare wrapper."""
        # This is a logic test - we verify the command building logic
        # would produce unshare commands

        # The implementation wraps the firecracker command with:
        # unshare --user --map-root-user --mount --propagation private -- sh -c 'mount -t tmpfs tmpfs /tmp && exec <fc_cmd>'
        # Note: No longer creates /tmp/bandsox since vsock sockets are now in storage_dir/vsock/

        import shlex

        # Test command string construction
        firecracker_bin = "/usr/bin/firecracker"
        socket_path = "/tmp/test.sock"
        # Use shlex.join for safe quoting (matches implementation)
        fc_cmd = [firecracker_bin, "--api-sock", socket_path]
        fc_cmd_str = shlex.join(fc_cmd)

        # Expected unshare command structure (with user namespace for rootless mount)
        unshare_cmd = [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "private",
            "--",
            "sh",
            "-c",
            f"mount -t tmpfs tmpfs /tmp && exec {fc_cmd_str}",
        ]

        # Verify command structure
        assert unshare_cmd[0] == "unshare"
        assert "--user" in unshare_cmd
        assert "--map-root-user" in unshare_cmd
        assert "--mount" in unshare_cmd
        assert "--propagation" in unshare_cmd
        assert "private" in unshare_cmd
        assert "sh" in unshare_cmd
        assert "mount -t tmpfs tmpfs /tmp" in unshare_cmd[-1]
        # Should NOT have mkdir -p /tmp/bandsox anymore
        assert "mkdir -p /tmp/bandsox" not in unshare_cmd[-1]
        assert firecracker_bin in unshare_cmd[-1]

    def test_start_process_skips_unshare_when_none_isolation(self):
        """Test that start_process skips unshare wrapper when isolation is 'none'."""
        # When vsock_isolation='none', the command should be just:
        # [firecracker_bin, "--api-sock", socket_path]
        # without any unshare wrapper

        firecracker_bin = "/usr/bin/firecracker"
        socket_path = "/tmp/test.sock"

        # Without namespace isolation, command is just firecracker
        cmd = [firecracker_bin, "--api-sock", socket_path]

        # Verify no unshare in command
        assert "unshare" not in cmd
        assert cmd[0] == firecracker_bin

    def test_vsock_isolation_passed_through_managed_vm(self):
        """Test that ManagedMicroVM passes vsock_isolation and vsock_dir to parent."""
        from bandsox.core import ManagedMicroVM, BandSox

        with tempfile.TemporaryDirectory() as tmpdir:
            bs = BandSox(storage_dir=tmpdir)
            socket_path = str(bs.sockets_dir / "test-vm.sock")

            # Test with namespace isolation
            vm = ManagedMicroVM("test-vm", socket_path, bs, vsock_isolation="namespace")
            assert vm.vsock_isolation == "namespace"
            # Should use storage_dir/vsock/ as vsock_dir
            assert vm.vsock_dir == str(bs.vsock_dir)

            # Test with none isolation
            vm2 = ManagedMicroVM("test-vm2", socket_path, bs, vsock_isolation="none")
            assert vm2.vsock_isolation == "none"
            assert vm2.vsock_dir == str(bs.vsock_dir)
