import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bandsox.server as server


class DummyVM:
    def __init__(self, vm_id: str, download_content: bytes = b"hello"):
        self.vm_id = vm_id
        self.download_content = download_content
        self.files = [{"name": "etc", "type": "directory", "size": 0, "mtime": 1}]
        self.stopped = False
        self.paused = False
        self.resumed = False
        self.fail_pause = False
        self.fail_resume = False
        self.inputs = []
        self.resizes = []
        self.killed_sessions = []

    def stop(self):
        self.stopped = True

    def pause(self):
        if self.fail_pause:
            raise Exception("Connection refused")
        self.paused = True

    def resume(self):
        if self.fail_resume:
            raise Exception("Connection refused")
        self.resumed = True

    def list_dir(self, path="/"):
        return self.files

    def download_file(self, remote, local):
        Path(local).write_bytes(self.download_content)

    def start_pty_session(self, cmd, cols, rows, on_stdout=None, on_exit=None):
        if on_stdout:
            on_stdout("output")
        return "session-1"

    def send_session_input(self, session_id, data, encoding="base64"):
        self.inputs.append((session_id, data, encoding))

    def resize_session(self, session_id, cols, rows):
        self.resizes.append((session_id, cols, rows))

    def kill_session(self, session_id):
        self.killed_sessions.append(session_id)


class FakeBandSox:
    def __init__(self, tmp_path: Path):
        self.vm = DummyVM("vm-123")
        self.vms = [{"id": self.vm.vm_id}]
        self.snapshots = [{"id": "snap-1"}]
        self.vm_info = {"id": self.vm.vm_id, "rootfs_path": str(tmp_path / "rootfs.ext4")}
        self.deleted_snapshot = None
        self.deleted_vm = None
        self.last_status = None

    def list_vms(self):
        return self.vms

    def list_snapshots(self):
        return self.snapshots

    def create_vm(self, *_, **__):
        return self.vm

    def restore_vm(self, snapshot_id, name=None, enable_networking=True):
        if snapshot_id == "missing":
            raise FileNotFoundError("snapshot missing")
        return self.vm

    def delete_snapshot(self, snapshot_id):
        if snapshot_id == "missing":
            raise FileNotFoundError("snapshot missing")
        self.deleted_snapshot = snapshot_id

    def get_vm(self, vm_id):
        return self.vm if vm_id == self.vm.vm_id else None

    def update_vm_status(self, vm_id, status):
        self.last_status = (vm_id, status)

    def snapshot_vm(self, vm, snapshot_name=None):
        return snapshot_name or "snapshot-auto"

    def delete_vm(self, vm_id):
        self.deleted_vm = vm_id

    def get_vm_info(self, vm_id):
        return self.vm_info if vm_id == self.vm.vm_id else None


@pytest.fixture
def fake_bs(tmp_path, monkeypatch):
    fake = FakeBandSox(tmp_path)
    monkeypatch.setattr(server, "bs", fake)
    return fake


@pytest.fixture
def client(fake_bs):
    return TestClient(server.app)


@pytest.mark.parametrize("path", ["/", "/vm_details", "/terminal", "/markdown_viewer"])
def test_static_pages(client, path):
    resp = client.get(path)
    assert resp.status_code == 200


def test_list_vms(client, fake_bs):
    resp = client.get("/api/vms")
    assert resp.status_code == 200
    assert resp.json() == fake_bs.vms


def test_list_snapshots(client, fake_bs):
    resp = client.get("/api/snapshots")
    assert resp.status_code == 200
    assert resp.json() == fake_bs.snapshots


def test_create_vm_success(client, fake_bs):
    payload = {
        "image": "alpine:latest",
        "name": "unit-test",
        "vcpu": 2,
        "mem_mib": 256,
        "enable_networking": False,
        "force_rebuild": False,
        "disk_size_mib": 1024,
    }
    resp = client.post("/api/vms", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"id": fake_bs.vm.vm_id, "status": "created"}


def test_create_vm_failure(client, fake_bs, monkeypatch):
    def fail(*_, **__):
        raise Exception("boom")

    monkeypatch.setattr(fake_bs, "create_vm", fail)
    resp = client.post("/api/vms", json={"image": "alpine"})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "boom"


def test_restore_snapshot_success(client, fake_bs):
    resp = client.post("/api/snapshots/snap-1/restore", json={"name": "restored", "enable_networking": False})
    assert resp.status_code == 200
    assert resp.json() == {"id": fake_bs.vm.vm_id, "status": "restored"}


def test_restore_snapshot_not_found(client, fake_bs, monkeypatch):
    def missing(*_, **__):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(fake_bs, "restore_vm", missing)
    resp = client.post("/api/snapshots/missing/restore", json={"name": "test", "enable_networking": True})
    assert resp.status_code == 404


def test_delete_snapshot_success(client, fake_bs):
    resp = client.delete("/api/snapshots/snap-1")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    assert fake_bs.deleted_snapshot == "snap-1"


def test_delete_snapshot_not_found(client, fake_bs, monkeypatch):
    def missing(snapshot_id):
        raise FileNotFoundError(f"{snapshot_id} missing")

    monkeypatch.setattr(fake_bs, "delete_snapshot", missing)
    resp = client.delete("/api/snapshots/missing")
    assert resp.status_code == 404


def test_stop_vm_success(client, fake_bs):
    resp = client.post(f"/api/vms/{fake_bs.vm.vm_id}/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "stopped"}
    assert fake_bs.vm.stopped is True


def test_stop_vm_not_found(client):
    resp = client.post("/api/vms/unknown/stop")
    assert resp.status_code == 404


def test_pause_vm_success(client, fake_bs):
    resp = client.post(f"/api/vms/{fake_bs.vm.vm_id}/pause")
    assert resp.status_code == 200
    assert resp.json() == {"status": "paused"}
    assert fake_bs.vm.paused is True


def test_pause_vm_not_running(client, fake_bs):
    fake_bs.vm.fail_pause = True
    resp = client.post(f"/api/vms/{fake_bs.vm.vm_id}/pause")
    assert resp.status_code == 409
    assert fake_bs.last_status == (fake_bs.vm.vm_id, "stopped")


def test_resume_vm_success(client, fake_bs):
    resp = client.post(f"/api/vms/{fake_bs.vm.vm_id}/resume")
    assert resp.status_code == 200
    assert resp.json() == {"status": "resumed"}
    assert fake_bs.vm.resumed is True


def test_resume_vm_not_running(client, fake_bs):
    fake_bs.vm.fail_resume = True
    resp = client.post(f"/api/vms/{fake_bs.vm.vm_id}/resume")
    assert resp.status_code == 409
    assert fake_bs.last_status == (fake_bs.vm.vm_id, "stopped")


def test_delete_vm(client, fake_bs):
    resp = client.delete(f"/api/vms/{fake_bs.vm.vm_id}")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    assert fake_bs.deleted_vm == fake_bs.vm.vm_id


def test_snapshot_vm_success(client, fake_bs):
    resp = client.post(f"/api/vms/{fake_bs.vm.vm_id}/snapshot", json={"name": "snap-new"})
    assert resp.status_code == 200
    assert resp.json() == {"snapshot_id": "snap-new"}


def test_snapshot_vm_not_found(client):
    resp = client.post("/api/vms/unknown/snapshot", json={"name": "snap-new"})
    assert resp.status_code == 404


def test_get_vm_details_success(client, fake_bs):
    resp = client.get(f"/api/vms/{fake_bs.vm.vm_id}")
    assert resp.status_code == 200
    assert resp.json() == fake_bs.vm_info


def test_get_vm_details_not_found(client):
    resp = client.get("/api/vms/unknown")
    assert resp.status_code == 404


def test_list_directory(client, fake_bs):
    resp = client.get(f"/api/vms/{fake_bs.vm.vm_id}/files", params={"path": "/etc"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["path"] == "/etc"
    assert payload["files"][0]["name"] == "etc"
    assert payload["files"][0]["is_dir"] is True


def test_download_file(client, fake_bs):
    resp = client.get(f"/api/vms/{fake_bs.vm.vm_id}/download", params={"path": "/tmp/out.txt"})
    assert resp.status_code == 200
    assert resp.content == fake_bs.vm.download_content
    assert "attachment" in resp.headers["content-disposition"]


def test_terminal_websocket(client, fake_bs):
    with client.websocket_connect(f"/api/vms/{fake_bs.vm.vm_id}/terminal?cols=80&rows=24") as ws:
        message = ws.receive_text()
        assert message == "output"
        ws.send_text(json.dumps({"type": "input", "data": "Zm9v"}))
    assert fake_bs.vm.inputs == [("session-1", "Zm9v", "base64")]
    assert fake_bs.vm.killed_sessions == ["session-1"]

