# ruff: noqa: E402

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.firecracker import FirecrackerClient
from bandsox.recording import RecordingError, RecordingManager


EMPTY_TRACE_HASH = "0" * 64


class LiveFirecracker:
    def __init__(self, binary: Path, tmp_path: Path, name: str):
        self.socket_path = tmp_path / f"{name}.sock"
        self.process = subprocess.Popen(
            [str(binary), "--api-sock", str(self.socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.client = FirecrackerClient(str(self.socket_path))
        if not self.client.wait_for_socket(timeout=5):
            stderr = ""
            try:
                _, stderr = self.process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            self.close()
            raise RuntimeError(f"Firecracker API socket was not created: {stderr}")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


class LiveVM:
    def __init__(self, vm_id: str, firecracker: LiveFirecracker):
        self.vm_id = vm_id
        self.client = firecracker.client
        self._firecracker = firecracker

    def close(self):
        self._firecracker.close()


class LiveReplayBandSox:
    def __init__(self, tmp_path: Path, firecracker_binary: Path):
        self.storage_dir = tmp_path
        self.snapshots_dir = tmp_path / "snapshots"
        self.snapshots_dir.mkdir()
        self.firecracker_binary = firecracker_binary
        self.restored = []
        self._vms = []
        self.metadata = {
            "record-vm": {
                "id": "record-vm",
                "name": "record-vm",
                "image": "api-only",
                "vcpu": 1,
                "mem_mib": 128,
                "status": "created",
                "metadata": {},
            }
        }

    def create_vm(self, vm_id: str) -> LiveVM:
        vm = LiveVM(
            vm_id,
            LiveFirecracker(self.firecracker_binary, Path(self.storage_dir), vm_id),
        )
        self._vms.append(vm)
        return vm

    def close(self):
        for vm in reversed(self._vms):
            vm.close()

    def get_vm_info(self, vm_id):
        return self.metadata.get(vm_id)

    def update_vm_metadata(self, vm_id, metadata):
        self.metadata[vm_id]["metadata"] = metadata

    def snapshot_vm(self, vm, snapshot_name=None, metadata=None):
        snapshot_id = snapshot_name or "snapshot-1"
        snap_dir = self.snapshots_dir / snapshot_id
        snap_dir.mkdir()
        (snap_dir / "metadata.json").write_text(json.dumps({"metadata": metadata or {}}))
        return snapshot_id

    def restore_vm(self, snapshot_id, **kwargs):
        self.restored.append((snapshot_id, kwargs))
        vm_id = f"replay-vm-{len(self.restored)}"
        self.metadata[vm_id] = {
            "id": vm_id,
            "name": kwargs.get("name"),
            "status": "created",
            "metadata": kwargs.get("metadata") or {},
        }
        vm = self.create_vm(vm_id)
        replay_config = kwargs.get("replay_config")
        if replay_config:
            vm.client.put_replay_config(replay_config)
        return vm


def test_recording_manager_accepts_real_firecracker_replay_api(tmp_path):
    binary = os.environ.get("BANDSOX_FIRECRACKER_BIN")
    if not binary:
        pytest.skip("BANDSOX_FIRECRACKER_BIN is required for the live replay API test")
    firecracker_binary = Path(binary)
    if not firecracker_binary.exists():
        pytest.skip(f"Firecracker binary not found: {firecracker_binary}")

    bandsox = LiveReplayBandSox(tmp_path, firecracker_binary)
    manager = RecordingManager(bandsox)
    vm = bandsox.create_vm("record-vm")

    try:
        recording = manager.start_recording(
            vm,
            name="live-api",
            replay_profile="quantum",
            precision="quantum",
            strict_engine=True,
        )
        checkpoint = manager.checkpoint(recording["id"], vm, name="cursor")
        replay = manager.replay(
            recording["id"],
            checkpoint_id=checkpoint["id"],
            name="strict-replay",
            strict_engine=True,
        )

        record_log = tmp_path / "recordings" / recording["id"] / "events.replaylog"
        assert recording["engine_status"] == "configured"
        assert record_log.exists()
        assert checkpoint["trace_events_recorded"] == 0
        assert checkpoint["trace_hash"] == EMPTY_TRACE_HASH
        assert bandsox.restored[-1][1]["replay_config"]["trace_start_seq"] == 0
        assert bandsox.restored[-1][1]["replay_config"]["trace_start_hash"] == EMPTY_TRACE_HASH
        assert replay["engine_status"] == "configured"
        assert replay["verification_status"] == "verified"
        assert replay["verification_reasons"] == []
        assert replay["trace_hash"] == checkpoint["trace_hash"]
        assert replay["trace_events_replayed"] == checkpoint["trace_events_recorded"]

        timeline_types = [event["type"] for event in manager.timeline(recording["id"])]
        assert timeline_types == [
            "recording.started",
            "checkpoint.created",
            "replay.started",
        ]
    finally:
        bandsox.close()


def test_recording_manager_rejects_replay_cursor_mismatch_with_real_firecracker_api(tmp_path):
    binary = os.environ.get("BANDSOX_FIRECRACKER_BIN")
    if not binary:
        pytest.skip("BANDSOX_FIRECRACKER_BIN is required for the live replay API test")
    firecracker_binary = Path(binary)
    if not firecracker_binary.exists():
        pytest.skip(f"Firecracker binary not found: {firecracker_binary}")

    bandsox = LiveReplayBandSox(tmp_path, firecracker_binary)
    manager = RecordingManager(bandsox)
    vm = bandsox.create_vm("record-vm")

    try:
        recording = manager.start_recording(
            vm,
            name="live-api-mismatch",
            replay_profile="quantum",
            precision="quantum",
            strict_engine=True,
        )
        checkpoint = manager.checkpoint(recording["id"], vm, name="cursor")

        manifest = manager._load_manifest(recording["id"])
        manifest["checkpoints"][0]["trace_hash"] = "f" * 64
        manager._save_manifest(manifest)

        with pytest.raises(RecordingError, match="trace start hash mismatch"):
            manager.replay(
                recording["id"],
                checkpoint_id=checkpoint["id"],
                name="strict-replay-mismatch",
                strict_engine=True,
            )
    finally:
        bandsox.close()


def test_firecracker_replay_api_rejects_unsupported_profile(tmp_path):
    binary = os.environ.get("BANDSOX_FIRECRACKER_BIN")
    if not binary:
        pytest.skip("BANDSOX_FIRECRACKER_BIN is required for the live replay API test")
    firecracker_binary = Path(binary)
    if not firecracker_binary.exists():
        pytest.skip(f"Firecracker binary not found: {firecracker_binary}")

    firecracker = LiveFirecracker(firecracker_binary, tmp_path, "bad-profile")
    try:
        with pytest.raises(Exception, match="unsupported replay profile"):
            firecracker.client.put_replay_config(
                {
                    "mode": "record",
                    "log_path": str(tmp_path / "events.replaylog"),
                    "profile": "not-a-real-replay-profile",
                    "precision": "quantum",
                }
            )
    finally:
        firecracker.close()


def test_firecracker_replay_api_rejects_missing_strict_log(tmp_path):
    binary = os.environ.get("BANDSOX_FIRECRACKER_BIN")
    if not binary:
        pytest.skip("BANDSOX_FIRECRACKER_BIN is required for the live replay API test")
    firecracker_binary = Path(binary)
    if not firecracker_binary.exists():
        pytest.skip(f"Firecracker binary not found: {firecracker_binary}")

    firecracker = LiveFirecracker(firecracker_binary, tmp_path, "missing-log")
    try:
        with pytest.raises(Exception, match="strict replay log is missing"):
            firecracker.client.put_replay_config(
                {
                    "mode": "replay",
                    "log_path": str(tmp_path / "missing.replaylog"),
                    "profile": "quantum",
                    "precision": "quantum",
                    "strict": True,
                }
            )
    finally:
        firecracker.close()
