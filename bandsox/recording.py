"""Flight-recorder orchestration for BandSox microVMs.

This module owns host-side recording state. Firecracker remains responsible
for VM execution and snapshots; BandSox owns manifests, checkpoint catalogues,
timeline events, branches, and replay launch policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import Any


MANIFEST_VERSION = 1
ACTIVE_RECORDING_METADATA_KEY = "bandsox_active_recording_id"


class RecordingError(Exception):
    """Raised when a recording operation cannot be completed."""


def _now() -> float:
    return time.time()


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=_json_default)
    os.replace(tmp_path, path)


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)


def _safe_slug(value: str) -> str:
    allowed = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            allowed.append(ch)
        else:
            allowed.append("-")
    slug = "".join(allowed).strip("-._")
    return slug[:96] or "recording"


def _event_hash(event: dict[str, Any]) -> str:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RecordingManager:
    def __init__(self, bandsox):
        self.bandsox = bandsox
        self.recordings_dir = Path(bandsox.storage_dir) / "recordings"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def _recording_dir(self, recording_id: str) -> Path:
        return self.recordings_dir / recording_id

    def _manifest_path(self, recording_id: str) -> Path:
        return self._recording_dir(recording_id) / "manifest.json"

    def _timeline_path(self, recording_id: str) -> Path:
        return self._recording_dir(recording_id) / "timeline.jsonl"

    def _load_manifest(self, recording_id: str) -> dict[str, Any]:
        path = self._manifest_path(recording_id)
        if not path.exists():
            raise FileNotFoundError(f"Recording {recording_id} not found")
        return _load_json(path)

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = _now()
        _atomic_write_json(self._manifest_path(manifest["id"]), manifest)

    def _vm_recording_id(self, vm_id: str) -> str | None:
        info = self.bandsox.get_vm_info(vm_id) or {}
        metadata = info.get("metadata") or {}
        value = metadata.get(ACTIVE_RECORDING_METADATA_KEY)
        return str(value) if value else None

    def _set_vm_recording_id(self, vm_id: str, recording_id: str | None) -> None:
        info = self.bandsox.get_vm_info(vm_id) or {}
        metadata = dict(info.get("metadata") or {})
        if recording_id:
            metadata[ACTIVE_RECORDING_METADATA_KEY] = recording_id
        else:
            metadata.pop(ACTIVE_RECORDING_METADATA_KEY, None)
        self.bandsox.update_vm_metadata(vm_id, metadata)

    def _snapshot_log_offsets(self, vm_id: str) -> dict[str, Any]:
        log_file = Path(self.bandsox.storage_dir) / "logs" / f"{vm_id}.log"
        if not log_file.exists():
            return {}
        return {"runner_log": {"path": str(log_file), "bytes": log_file.stat().st_size}}

    def _replay_status(self, vm) -> dict[str, Any] | None:
        try:
            return vm.client.get_replay_status()
        except Exception:
            return None

    def _replay_verification(
        self, checkpoint: dict[str, Any], replay_status: dict[str, Any] | None
    ) -> dict[str, Any]:
        checks = {
            "configured": False,
            "mode": False,
            "checkpoint_id": False,
            "trace_events_replayed": False,
            "trace_hash": False,
            "last_mismatch": False,
        }
        reasons = []

        if not replay_status:
            reasons.append("missing_firecracker_replay_status")
            return {
                "status": "unverified",
                "checks": checks,
                "reasons": reasons,
            }

        checks["configured"] = replay_status.get("configured") is True
        if not checks["configured"]:
            reasons.append("firecracker_replay_not_configured")

        mode = replay_status.get("mode")
        checks["mode"] = mode in (None, "replay")
        if not checks["mode"]:
            reasons.append(f"unexpected_replay_mode:{mode}")

        status_checkpoint_id = replay_status.get("checkpoint_id")
        checks["checkpoint_id"] = status_checkpoint_id in (None, checkpoint.get("id"))
        if not checks["checkpoint_id"]:
            reasons.append("checkpoint_id_mismatch")

        checkpoint_events = checkpoint.get("trace_events_recorded")
        replayed_events = replay_status.get("trace_events_replayed")
        checks["trace_events_replayed"] = (
            checkpoint_events is not None and replayed_events == checkpoint_events
        )
        if not checks["trace_events_replayed"]:
            reasons.append("trace_events_replayed_mismatch")

        checkpoint_hash = checkpoint.get("trace_hash")
        replay_hash = replay_status.get("trace_hash")
        checks["trace_hash"] = checkpoint_hash is not None and replay_hash == checkpoint_hash
        if not checks["trace_hash"]:
            reasons.append("trace_hash_mismatch")

        checks["last_mismatch"] = not replay_status.get("last_mismatch")
        if not checks["last_mismatch"]:
            reasons.append("firecracker_reported_replay_mismatch")

        return {
            "status": "verified" if not reasons else "trace_loaded",
            "checks": checks,
            "reasons": reasons,
        }

    def _run_probe_command(
        self, vm, command: str, timeout: int | float | None = None
    ) -> dict[str, Any]:
        stdout = []
        stderr = []
        exit_code = vm.exec_command(
            command,
            on_stdout=lambda chunk: stdout.append(str(chunk)),
            on_stderr=lambda chunk: stderr.append(str(chunk)),
            timeout=timeout or 30,
        )
        stdout_text = "".join(stdout)
        stderr_text = "".join(stderr)
        return {
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_sha256": _text_hash(stdout_text),
            "stderr_sha256": _text_hash(stderr_text),
        }

    def _capture_output_probe(self, vm, probe: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(probe, dict):
            raise RecordingError("Verification probe must be an object")
        probe_type = str(probe.get("type") or "command")
        name = str(probe.get("name") or probe.get("path") or probe.get("command") or probe_type)

        if probe_type == "command":
            command = probe.get("command")
            if not command:
                raise RecordingError(f"Command probe {name!r} is missing command")
            result = self._run_probe_command(vm, str(command), probe.get("timeout"))
            return {
                "type": "command",
                "name": name,
                "command": str(command),
                "timeout": probe.get("timeout"),
                "result": result,
            }

        if probe_type == "file_sha256":
            path = probe.get("path")
            if not path:
                raise RecordingError(f"File probe {name!r} is missing path")
            path = str(path)
            try:
                content = vm.get_file_contents(path)
                content = str(content)
                result = {
                    "exists": True,
                    "size_bytes": len(content.encode("utf-8")),
                    "sha256": _text_hash(content),
                    "error": None,
                }
            except Exception as exc:
                result = {
                    "exists": False,
                    "size_bytes": None,
                    "sha256": None,
                    "error": str(exc),
                }
            return {
                "type": "file_sha256",
                "name": name,
                "path": path,
                "result": result,
            }

        if probe_type == "guest_file_sha256":
            path = probe.get("path")
            if not path:
                raise RecordingError(f"Guest file probe {name!r} is missing path")
            path = str(path)
            quoted = shlex.quote(path)
            command = (
                f"if [ -e {quoted} ]; then "
                f"printf 'present\\n'; wc -c < {quoted}; sha256sum {quoted} | awk '{{print $1}}'; "
                "else printf 'missing\\n'; fi"
            )
            command_result = self._run_probe_command(vm, command, probe.get("timeout"))
            lines = [line.strip() for line in command_result["stdout"].splitlines()]
            exists = bool(lines and lines[0] == "present")
            result = {
                "exists": exists,
                "size_bytes": int(lines[1]) if exists and len(lines) > 1 else None,
                "sha256": lines[2] if exists and len(lines) > 2 else None,
                "exit_code": command_result["exit_code"],
                "stderr": command_result["stderr"],
            }
            return {
                "type": "guest_file_sha256",
                "name": name,
                "path": path,
                "result": result,
            }

        raise RecordingError(f"Unsupported verification probe type: {probe_type}")

    def _capture_output_equivalence(
        self, vm, verification_probes: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        if not verification_probes:
            return {"status": "not_configured", "probes": []}
        captures = []
        for probe in verification_probes:
            captures.append(self._capture_output_probe(vm, probe))
        return {
            "status": "captured",
            "captured_at": _now(),
            "probes": captures,
        }

    def _compare_output_probe(
        self, expected: dict[str, Any], actual: dict[str, Any]
    ) -> dict[str, Any]:
        reasons = []
        probe_type = expected.get("type")
        expected_result = expected.get("result") or {}
        actual_result = actual.get("result") or {}

        if expected.get("type") != actual.get("type"):
            reasons.append("probe_type_mismatch")
        if expected.get("name") != actual.get("name"):
            reasons.append("probe_name_mismatch")

        if probe_type == "command":
            for field in ("exit_code", "stdout_sha256", "stderr_sha256", "stdout", "stderr"):
                if expected_result.get(field) != actual_result.get(field):
                    reasons.append(f"{field}_mismatch")
        elif probe_type in ("file_sha256", "guest_file_sha256"):
            for field in ("exists", "size_bytes", "sha256"):
                if expected_result.get(field) != actual_result.get(field):
                    reasons.append(f"{field}_mismatch")
        else:
            reasons.append(f"unsupported_probe_type:{probe_type}")

        return {
            "type": probe_type,
            "name": expected.get("name"),
            "passed": not reasons,
            "reasons": reasons,
            "expected": expected_result,
            "actual": actual_result,
        }

    def _verify_output_equivalence(
        self, vm, expected_equivalence: dict[str, Any] | None
    ) -> dict[str, Any]:
        expected_probes = (expected_equivalence or {}).get("probes") or []
        if not expected_probes:
            return {"status": "not_configured", "checks": []}

        actual_probes = []
        checks = []
        for expected in expected_probes:
            actual = self._capture_output_probe(vm, expected)
            actual_probes.append(actual)
            checks.append(self._compare_output_probe(expected, actual))

        failed = [check for check in checks if not check["passed"]]
        return {
            "status": "failed" if failed else "passed",
            "checked_at": _now(),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
            "checks": checks,
            "actual_probes": actual_probes,
        }

    def _merge_output_verification(
        self, verification: dict[str, Any], output_equivalence: dict[str, Any]
    ) -> dict[str, Any]:
        verification = {
            "status": verification["status"],
            "checks": dict(verification["checks"]),
            "reasons": list(verification["reasons"]),
        }
        status = output_equivalence.get("status")
        if status == "not_configured":
            verification["checks"]["output_equivalence"] = None
        elif status == "passed":
            verification["checks"]["output_equivalence"] = True
        else:
            verification["checks"]["output_equivalence"] = False
            verification["reasons"].append("output_equivalence_failed")
            if verification["status"] == "verified":
                verification["status"] = "output_mismatch"
        return verification

    def _base_manifest(
        self,
        vm_id: str,
        name: str | None,
        metadata: dict[str, Any] | None,
        replay_profile: str,
        precision: str,
    ) -> dict[str, Any]:
        vm_info = self.bandsox.get_vm_info(vm_id) or {}
        return {
            "manifest_version": MANIFEST_VERSION,
            "id": uuid.uuid4().hex,
            "name": name,
            "source_vm_id": vm_id,
            "current_vm_id": vm_id,
            "status": "recording",
            "created_at": _now(),
            "updated_at": _now(),
            "metadata": metadata or {},
            "replay_profile": replay_profile,
            "precision": precision,
            "guarantee_class": "unverified-deterministic",
            "verification_status": "unverified",
            "engine_status": "pending",
            "engine_error": None,
            "firecracker_replay": {
                "fork_required": True,
                "record_log": "events.replaylog",
                "mode": "record",
            },
            "vm": {
                "id": vm_id,
                "name": vm_info.get("name"),
                "image": vm_info.get("image"),
                "vcpu": vm_info.get("vcpu"),
                "mem_mib": vm_info.get("mem_mib"),
                "network_config": vm_info.get("network_config"),
                "vsock_config": vm_info.get("vsock_config"),
                "status": vm_info.get("status"),
            },
            "checkpoints": [],
            "branches": [],
            "replays": [],
            "next_event_seq": 0,
            "last_event_hash": None,
        }

    def list_recordings(self) -> list[dict[str, Any]]:
        recordings = []
        for path in self.recordings_dir.glob("*/manifest.json"):
            try:
                recordings.append(_load_json(path))
            except Exception:
                recordings.append(
                    {
                        "id": path.parent.name,
                        "status": "metadata_corrupted",
                        "path": str(path.parent),
                    }
                )
        recordings.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return recordings

    def get_recording(self, recording_id: str) -> dict[str, Any]:
        return self._load_manifest(recording_id)

    def start_recording(
        self,
        vm,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        replay_profile: str = "quantum",
        precision: str = "quantum",
        strict_engine: bool = False,
    ) -> dict[str, Any]:
        manifest = self._base_manifest(
            vm.vm_id,
            name=name,
            metadata=metadata,
            replay_profile=replay_profile,
            precision=precision,
        )
        root = self._recording_dir(manifest["id"])
        for rel in ("checkpoints", "logs", "metrics", "pcap"):
            (root / rel).mkdir(parents=True, exist_ok=True)

        replay_config = {
            "mode": "record",
            "log_path": str(root / "events.replaylog"),
            "profile": replay_profile,
            "precision": precision,
        }

        try:
            vm.client.put_replay_config(replay_config)
            manifest["engine_status"] = "configured"
            manifest["firecracker_replay"]["status"] = self._replay_status(vm)
        except Exception as exc:
            if strict_engine:
                shutil.rmtree(root, ignore_errors=True)
                raise RecordingError(
                    f"Firecracker deterministic replay engine unavailable: {exc}"
                ) from exc
            manifest["engine_status"] = "unavailable"
            manifest["engine_error"] = str(exc)

        self._save_manifest(manifest)
        self._set_vm_recording_id(vm.vm_id, manifest["id"])
        self.append_event(
            manifest["id"],
            "recording.started",
            {
                "vm_id": vm.vm_id,
                "replay_profile": replay_profile,
                "precision": precision,
                "engine_status": manifest["engine_status"],
            },
        )
        return self._load_manifest(manifest["id"])

    def append_event(
        self,
        recording_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        vm_id: str | None = None,
    ) -> dict[str, Any]:
        manifest = self._load_manifest(recording_id)
        event = {
            "seq": manifest.get("next_event_seq", 0),
            "ts": _now(),
            "type": event_type,
            "vm_id": vm_id or manifest.get("current_vm_id"),
            "payload": payload or {},
            "prev_hash": manifest.get("last_event_hash"),
        }
        event["hash"] = _event_hash(event)

        with open(self._timeline_path(recording_id), "a") as f:
            f.write(json.dumps(event, sort_keys=True, default=_json_default) + "\n")

        manifest["next_event_seq"] = event["seq"] + 1
        manifest["last_event_hash"] = event["hash"]
        self._save_manifest(manifest)
        return event

    def append_event_for_vm(
        self, vm_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        recording_id = self._vm_recording_id(vm_id)
        if not recording_id:
            return None
        try:
            return self.append_event(recording_id, event_type, payload, vm_id=vm_id)
        except FileNotFoundError:
            self._set_vm_recording_id(vm_id, None)
            return None

    def checkpoint(
        self,
        recording_id: str,
        vm,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        verification_probes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        manifest = self._load_manifest(recording_id)
        ordinal = len(manifest.get("checkpoints", []))
        checkpoint_id = f"{recording_id[:12]}-{ordinal:06d}"
        snapshot_name = _safe_slug(name or f"recording-{checkpoint_id}")

        output_equivalence = self._capture_output_equivalence(vm, verification_probes)

        try:
            vm.client.post_replay_flush()
        except Exception:
            pass
        replay_status = self._replay_status(vm)

        snapshot_id = self.bandsox.snapshot_vm(
            vm,
            snapshot_name=snapshot_name,
            metadata={
                "recording_id": recording_id,
                "checkpoint_id": checkpoint_id,
                "checkpoint_ordinal": ordinal,
                "metadata": metadata or {},
            },
        )

        checkpoint_dir = self._recording_dir(recording_id) / "checkpoints" / f"{ordinal:06d}"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        snapshot_dir = self.bandsox.snapshots_dir / snapshot_id
        info = {
            "id": checkpoint_id,
            "ordinal": ordinal,
            "name": name or snapshot_name,
            "snapshot_id": snapshot_id,
            "snapshot_path": str(snapshot_dir),
            "recording_id": recording_id,
            "vm_id": vm.vm_id,
            "created_at": _now(),
            "metadata": metadata or {},
            "artifact_offsets": self._snapshot_log_offsets(vm.vm_id),
            "trace_hash": (replay_status or {}).get("trace_hash"),
            "trace_events_recorded": (replay_status or {}).get("trace_events_recorded"),
            "firecracker_replay_status": replay_status,
            "output_equivalence": output_equivalence,
            "event_seq": manifest.get("next_event_seq", 0),
            "event_hash": manifest.get("last_event_hash"),
        }
        _atomic_write_json(checkpoint_dir / "metadata.json", info)

        manifest.setdefault("checkpoints", []).append(info)
        manifest["current_vm_id"] = vm.vm_id
        self._save_manifest(manifest)
        self.append_event(recording_id, "checkpoint.created", info, vm_id=vm.vm_id)
        return info

    def find_checkpoint(self, checkpoint_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for manifest in self.list_recordings():
            for checkpoint in manifest.get("checkpoints", []):
                if checkpoint.get("id") == checkpoint_id:
                    return manifest, checkpoint
        raise FileNotFoundError(f"Checkpoint {checkpoint_id} not found")

    def branch_checkpoint(
        self,
        checkpoint_id: str,
        name: str | None = None,
        enable_networking: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest, checkpoint = self.find_checkpoint(checkpoint_id)
        branch_meta = {
            "parent_recording_id": manifest["id"],
            "branch_from_checkpoint_id": checkpoint_id,
            **(metadata or {}),
        }
        vm = self.bandsox.restore_vm(
            checkpoint["snapshot_id"],
            name=name or f"branch-{checkpoint_id}",
            enable_networking=enable_networking,
            metadata=branch_meta,
        )
        branch = {
            "id": uuid.uuid4().hex,
            "recording_id": manifest["id"],
            "checkpoint_id": checkpoint_id,
            "vm_id": vm.vm_id,
            "created_at": _now(),
            "name": name,
            "metadata": metadata or {},
        }
        manifest.setdefault("branches", []).append(branch)
        self._save_manifest(manifest)
        self.append_event(manifest["id"], "branch.created", branch, vm_id=vm.vm_id)
        return branch

    def replay(
        self,
        recording_id: str,
        checkpoint_id: str | None = None,
        name: str | None = None,
        enable_networking: bool = False,
        strict_engine: bool = True,
    ) -> dict[str, Any]:
        manifest = self._load_manifest(recording_id)
        checkpoints = manifest.get("checkpoints", [])
        if not checkpoints:
            raise RecordingError(f"Recording {recording_id} has no checkpoints")
        checkpoint = None
        if checkpoint_id:
            checkpoint = next((item for item in checkpoints if item["id"] == checkpoint_id), None)
            if checkpoint is None:
                raise FileNotFoundError(f"Checkpoint {checkpoint_id} not found")
        else:
            checkpoint = checkpoints[-1]

        root = self._recording_dir(recording_id)
        trace_start_seq = checkpoint.get("trace_events_recorded")
        trace_start_hash = checkpoint.get("trace_hash")
        if strict_engine and (trace_start_seq is None or trace_start_hash is None):
            raise RecordingError(
                f"Checkpoint {checkpoint['id']} does not include a Firecracker trace cursor"
            )

        replay_config = {
            "mode": "replay",
            "log_path": str(root / "events.replaylog"),
            "profile": manifest.get("replay_profile", "quantum"),
            "precision": manifest.get("precision", "quantum"),
            "checkpoint_id": checkpoint["id"],
            "strict": strict_engine,
        }
        if trace_start_seq is not None:
            replay_config["trace_start_seq"] = trace_start_seq
        if trace_start_hash is not None:
            replay_config["trace_start_hash"] = trace_start_hash
        restore_kwargs = {
            "name": name or f"replay-{checkpoint['id']}",
            "enable_networking": enable_networking,
            "metadata": {
                "replay_of_recording_id": recording_id,
                "replay_from_checkpoint_id": checkpoint["id"],
            },
        }
        if strict_engine:
            restore_kwargs["replay_config"] = replay_config

        try:
            vm = self.bandsox.restore_vm(checkpoint["snapshot_id"], **restore_kwargs)
        except Exception as exc:
            if strict_engine:
                raise RecordingError(
                    f"Deterministic replay failed before VM resume: {exc}"
                ) from exc
            raise
        replay_status = self._replay_status(vm)
        output_equivalence = self._verify_output_equivalence(
            vm, checkpoint.get("output_equivalence")
        )
        verification = self._merge_output_verification(
            self._replay_verification(checkpoint, replay_status),
            output_equivalence,
        )
        engine_status = "configured" if strict_engine else "not_requested"
        engine_error = None

        replay = {
            "id": uuid.uuid4().hex,
            "recording_id": recording_id,
            "checkpoint_id": checkpoint["id"],
            "vm_id": vm.vm_id,
            "created_at": _now(),
            "engine_status": engine_status,
            "engine_error": engine_error,
            "verification_status": verification["status"],
            "verification_checks": verification["checks"],
            "verification_reasons": verification["reasons"],
            "guarantee_class": (replay_status or {}).get(
                "guarantee_class", "unverified-deterministic"
            ),
            "trace_hash": (replay_status or {}).get("trace_hash"),
            "trace_events_replayed": (replay_status or {}).get("trace_events_replayed"),
            "firecracker_replay_status": replay_status,
            "output_equivalence": output_equivalence,
        }
        manifest.setdefault("replays", []).append(replay)
        self._save_manifest(manifest)
        self.append_event(recording_id, "replay.started", replay, vm_id=vm.vm_id)
        return replay

    def timeline(self, recording_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        self._load_manifest(recording_id)
        path = self._timeline_path(recording_id)
        if not path.exists():
            return []
        events = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        if limit is not None:
            events = events[-limit:]
        return events
