# ruff: noqa: E402

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox.recording import ACTIVE_RECORDING_METADATA_KEY, RecordingError, RecordingManager


class FakeFirecrackerClient:
    def __init__(self, fail_replay_config=False, replay_status=None):
        self.fail_replay_config = fail_replay_config
        self.replay_configs = []
        self.flushes = 0
        self.replay_status = replay_status or {
            "configured": True,
            "guarantee_class": "virtio-rng-inputs",
            "trace_hash": "0" * 64,
            "trace_events_recorded": 0,
            "trace_events_replayed": 0,
            "unsupported_reasons": ["virtio-net replay is not implemented"],
        }

    def put_replay_config(self, replay_config):
        if self.fail_replay_config:
            raise Exception("404 replay endpoint not found")
        self.replay_configs.append(replay_config)

    def post_replay_flush(self):
        self.flushes += 1
        self.replay_status = {
            **self.replay_status,
            "trace_hash": "a" * 64,
            "trace_events_recorded": 3,
        }

    def get_replay_status(self):
        return dict(self.replay_status)


class FakeVM:
    def __init__(
        self,
        vm_id="vm-1",
        fail_replay_config=False,
        replay_status=None,
        command_outputs=None,
        files=None,
    ):
        self.vm_id = vm_id
        self.client = FakeFirecrackerClient(
            fail_replay_config=fail_replay_config,
            replay_status=replay_status,
        )
        self.command_outputs = command_outputs or {}
        self.files = files or {}

    def exec_command(self, command, on_stdout=None, on_stderr=None, timeout=30):
        exit_code, stdout, stderr = self.command_outputs.get(command, (0, "", ""))
        if on_stdout and stdout:
            on_stdout(stdout)
        if on_stderr and stderr:
            on_stderr(stderr)
        return exit_code

    def get_file_contents(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakeBandSox:
    def __init__(self, tmp_path):
        self.storage_dir = tmp_path
        self.snapshots_dir = tmp_path / "snapshots"
        self.snapshots_dir.mkdir()
        self.metadata = {
            "vm-1": {
                "id": "vm-1",
                "name": "unit",
                "image": "alpine",
                "vcpu": 2,
                "mem_mib": 256,
                "status": "running",
                "metadata": {},
            }
        }
        self.snapshots = []
        self.restored = []
        self.replay_command_outputs = {}
        self.replay_files = {}

    def get_vm_info(self, vm_id):
        return self.metadata.get(vm_id)

    def update_vm_metadata(self, vm_id, metadata):
        self.metadata[vm_id]["metadata"] = metadata

    def snapshot_vm(self, vm, snapshot_name=None, metadata=None):
        snapshot_id = snapshot_name or "snap-1"
        snap_dir = self.snapshots_dir / snapshot_id
        snap_dir.mkdir()
        (snap_dir / "metadata.json").write_text(json.dumps({"metadata": metadata or {}}))
        self.snapshots.append((snapshot_id, metadata))
        return snapshot_id

    def restore_vm(self, snapshot_id, **kwargs):
        self.restored.append((snapshot_id, kwargs))
        vm_id = f"restored-{len(self.restored)}"
        self.metadata[vm_id] = {
            "id": vm_id,
            "name": kwargs.get("name"),
            "status": "running",
            "metadata": kwargs.get("metadata") or {},
        }
        return FakeVM(
            vm_id,
            command_outputs=self.replay_command_outputs,
            files=self.replay_files,
            replay_status={
                "configured": True,
                "guarantee_class": "virtio-rng-inputs",
                "trace_hash": "a" * 64,
                "trace_events_recorded": 0,
                "trace_events_replayed": 3,
                "unsupported_reasons": ["virtio-net replay is not implemented"],
            },
        )


def test_start_recording_writes_manifest_and_active_vm_metadata(tmp_path):
    bs = FakeBandSox(tmp_path)
    manager = RecordingManager(bs)
    vm = FakeVM()

    recording = manager.start_recording(
        vm,
        name="run-a",
        metadata={"owner": "unit"},
        replay_profile="quantum",
        precision="quantum",
    )

    assert recording["name"] == "run-a"
    assert recording["metadata"] == {"owner": "unit"}
    assert recording["engine_status"] == "configured"
    assert bs.metadata["vm-1"]["metadata"][ACTIVE_RECORDING_METADATA_KEY] == recording["id"]
    assert (tmp_path / "recordings" / recording["id"] / "manifest.json").exists()
    assert manager.timeline(recording["id"])[0]["type"] == "recording.started"
    assert vm.client.replay_configs[0]["mode"] == "record"
    assert recording["firecracker_replay"]["status"]["trace_hash"] == "0" * 64


def test_start_recording_can_continue_without_fork_when_not_strict(tmp_path):
    bs = FakeBandSox(tmp_path)
    manager = RecordingManager(bs)
    vm = FakeVM(fail_replay_config=True)

    recording = manager.start_recording(vm, strict_engine=False)

    assert recording["engine_status"] == "unavailable"
    assert "replay endpoint" in recording["engine_error"]


def test_start_recording_strict_requires_fork_endpoint(tmp_path):
    bs = FakeBandSox(tmp_path)
    manager = RecordingManager(bs)
    vm = FakeVM(fail_replay_config=True)

    with pytest.raises(Exception, match="deterministic replay engine unavailable"):
        manager.start_recording(vm, strict_engine=True)


def test_checkpoint_records_snapshot_reference_and_hashed_event_chain(tmp_path):
    bs = FakeBandSox(tmp_path)
    manager = RecordingManager(bs)
    vm = FakeVM()
    recording = manager.start_recording(vm)

    checkpoint = manager.checkpoint(
        recording["id"],
        vm,
        name="after-command",
        metadata={"phase": "test"},
    )

    assert checkpoint["name"] == "after-command"
    assert checkpoint["metadata"] == {"phase": "test"}
    assert checkpoint["trace_hash"] == "a" * 64
    assert checkpoint["trace_events_recorded"] == 3
    assert bs.snapshots[0][1]["recording_id"] == recording["id"]
    assert vm.client.flushes == 1

    timeline = manager.timeline(recording["id"])
    assert [event["type"] for event in timeline] == [
        "recording.started",
        "checkpoint.created",
    ]
    assert timeline[1]["prev_hash"] == timeline[0]["hash"]


def test_branch_and_replay_use_checkpoint_snapshot(tmp_path):
    bs = FakeBandSox(tmp_path)
    manager = RecordingManager(bs)
    vm = FakeVM()
    recording = manager.start_recording(vm)
    checkpoint = manager.checkpoint(recording["id"], vm)

    branch = manager.branch_checkpoint(
        checkpoint["id"],
        name="branch-a",
        enable_networking=False,
        metadata={"why": "unit"},
    )
    assert branch["checkpoint_id"] == checkpoint["id"]
    assert bs.restored[-1][0] == checkpoint["snapshot_id"]
    assert bs.restored[-1][1]["enable_networking"] is False

    replay = manager.replay(
        recording["id"],
        checkpoint_id=checkpoint["id"],
        name="replay-a",
        strict_engine=True,
    )
    assert replay["checkpoint_id"] == checkpoint["id"]
    assert bs.restored[-1][1]["replay_config"]["mode"] == "replay"
    assert bs.restored[-1][1]["replay_config"]["trace_start_seq"] == 3
    assert bs.restored[-1][1]["replay_config"]["trace_start_hash"] == "a" * 64
    assert replay["verification_status"] == "verified"
    assert replay["verification_reasons"] == []
    assert replay["trace_hash"] == checkpoint["trace_hash"]
    assert replay["trace_events_replayed"] == 3
    assert replay["output_equivalence"]["status"] == "not_configured"


def test_replay_verifies_command_output_and_file_hash_equivalence(tmp_path):
    bs = FakeBandSox(tmp_path)
    bs.replay_command_outputs = {"cat /workspace/result.txt": (0, "answer=42\n", "")}
    bs.replay_files = {"/workspace/result.txt": "answer=42\n"}
    manager = RecordingManager(bs)
    vm = FakeVM(
        command_outputs={"cat /workspace/result.txt": (0, "answer=42\n", "")},
        files={"/workspace/result.txt": "answer=42\n"},
    )
    recording = manager.start_recording(vm)
    checkpoint = manager.checkpoint(
        recording["id"],
        vm,
        verification_probes=[
            {
                "type": "command",
                "name": "result-command",
                "command": "cat /workspace/result.txt",
            },
            {
                "type": "file_sha256",
                "name": "result-file",
                "path": "/workspace/result.txt",
            },
        ],
    )

    assert checkpoint["output_equivalence"]["status"] == "captured"
    assert len(checkpoint["output_equivalence"]["probes"]) == 2

    replay = manager.replay(recording["id"], checkpoint_id=checkpoint["id"])

    assert replay["verification_status"] == "verified"
    assert replay["verification_checks"]["output_equivalence"] is True
    assert replay["output_equivalence"]["status"] == "passed"
    assert replay["output_equivalence"]["failed"] == 0


def test_replay_reports_output_mismatch_when_probe_output_changes(tmp_path):
    bs = FakeBandSox(tmp_path)
    bs.replay_command_outputs = {"cat /workspace/result.txt": (0, "answer=43\n", "")}
    bs.replay_files = {"/workspace/result.txt": "answer=43\n"}
    manager = RecordingManager(bs)
    vm = FakeVM(
        command_outputs={"cat /workspace/result.txt": (0, "answer=42\n", "")},
        files={"/workspace/result.txt": "answer=42\n"},
    )
    recording = manager.start_recording(vm)
    checkpoint = manager.checkpoint(
        recording["id"],
        vm,
        verification_probes=[
            {
                "type": "command",
                "name": "result-command",
                "command": "cat /workspace/result.txt",
            },
            {
                "type": "file_sha256",
                "name": "result-file",
                "path": "/workspace/result.txt",
            },
        ],
    )

    replay = manager.replay(recording["id"], checkpoint_id=checkpoint["id"])

    assert replay["verification_status"] == "output_mismatch"
    assert replay["verification_checks"]["output_equivalence"] is False
    assert "output_equivalence_failed" in replay["verification_reasons"]
    assert replay["output_equivalence"]["status"] == "failed"
    reasons = {
        reason
        for check in replay["output_equivalence"]["checks"]
        for reason in check["reasons"]
    }
    assert "stdout_sha256_mismatch" in reasons
    assert "sha256_mismatch" in reasons


def test_replay_reports_trace_loaded_when_checkpoint_cursor_does_not_verify(tmp_path):
    bs = FakeBandSox(tmp_path)
    manager = RecordingManager(bs)
    vm = FakeVM()
    recording = manager.start_recording(vm)
    checkpoint = manager.checkpoint(recording["id"], vm)

    manifest = manager._load_manifest(recording["id"])
    manifest["checkpoints"][0]["trace_hash"] = "b" * 64
    manager._save_manifest(manifest)

    replay = manager.replay(
        recording["id"],
        checkpoint_id=checkpoint["id"],
        name="replay-a",
        strict_engine=True,
    )

    assert replay["verification_status"] == "trace_loaded"
    assert replay["verification_checks"]["trace_hash"] is False
    assert replay["verification_reasons"] == ["trace_hash_mismatch"]


def test_strict_replay_requires_checkpoint_trace_cursor(tmp_path):
    bs = FakeBandSox(tmp_path)
    manager = RecordingManager(bs)
    vm = FakeVM()
    recording = manager.start_recording(vm)
    checkpoint = manager.checkpoint(recording["id"], vm)

    manifest = manager._load_manifest(recording["id"])
    manifest["checkpoints"][0].pop("trace_events_recorded")
    manager._save_manifest(manifest)

    with pytest.raises(RecordingError, match="does not include a Firecracker trace cursor"):
        manager.replay(recording["id"], checkpoint_id=checkpoint["id"], strict_engine=True)
