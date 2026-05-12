import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bandsox.server as server
import bandsox.vm as vm_module
from bandsox.core import BandSox, RemoteBandSox


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
        self.uploads = []
        self.text_writes = []
        self.byte_writes = []

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

    def exec_command(self, command, on_stdout=None, on_stderr=None, timeout=30):
        if on_stdout:
            on_stdout("stdout")
        if on_stderr:
            on_stderr("stderr")
        return 0

    def exec_python_capture(self, code, cwd="/tmp", packages=None, timeout=60, cleanup_venv=True):
        return {
            "exit_code": 0,
            "stdout": "pyout",
            "stderr": "",
            "output": "pyout",
            "success": True,
            "error": None,
        }

    def get_file_contents(self, path):
        return "file text"

    def upload_file(self, local, remote, append=False):
        self.uploads.append((Path(local).read_bytes(), remote, append))

    def write_text(self, remote, content, timeout=None, append=False):
        self.text_writes.append((remote, content, append))

    def append_text(self, remote, content, timeout=None):
        self.write_text(remote, content, timeout=timeout, append=True)

    def write_bytes(self, remote, content, timeout=None, append=False):
        self.byte_writes.append((remote, bytes(content), append))

    def get_file_info(self, path):
        return {"size": 9}

    def send_http_request(self, port, path="/", method="GET", **kwargs):
        return SimpleNamespace(
            status_code=201,
            headers={"x-test": "ok"},
            text=f"{method} {port}{path}",
        )

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

    def list_vms(self, limit=None, metadata_equals=None):
        return self.vms

    def list_snapshots(self):
        return self.snapshots

    def create_vm(self, *_, **__):
        return self.vm

    def restore_vm(self, snapshot_id, name=None, enable_networking=True, env_vars=None, metadata=None):
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

    def snapshot_vm(self, vm, snapshot_name=None, metadata=None):
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


def test_bandsox_constructor_accepts_server_url():
    assert isinstance(BandSox(server_url="http://localhost:8000"), RemoteBandSox)
    assert isinstance(BandSox("http://localhost:8000"), RemoteBandSox)


def test_list_vms(client, fake_bs):
    resp = client.get("/api/vms")
    assert resp.status_code == 200
    assert resp.json() == fake_bs.vms


def test_list_projects_alias(client, fake_bs):
    resp = client.get("/api/projects")
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


def test_exec_command(client, fake_bs):
    resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/exec",
        json={"command": "echo hi", "timeout": 5},
    )
    assert resp.status_code == 200
    assert resp.json() == {"exit_code": 0, "stdout": "stdout", "stderr": "stderr"}


def test_exec_python(client, fake_bs):
    resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/exec-python",
        json={"code": "print('hi')"},
    )
    assert resp.status_code == 200
    assert resp.json()["stdout"] == "pyout"


def test_read_write_file(client, fake_bs):
    read_resp = client.get(
        f"/api/vms/{fake_bs.vm.vm_id}/read-file",
        params={"path": "/tmp/in.txt"},
    )
    assert read_resp.status_code == 200
    assert read_resp.json() == {"path": "/tmp/in.txt", "content": "file text"}

    write_resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/write-file",
        json={"path": "/tmp/out.txt", "content": "hello"},
    )
    assert write_resp.status_code == 200
    assert fake_bs.vm.text_writes[-1] == ("/tmp/out.txt", "hello", False)


def test_read_file_falls_back_for_stopped_vm(client, fake_bs, monkeypatch):
    fake_bs.get_vm = lambda vm_id: None

    def fake_get_file_contents(self, path):
        assert self.rootfs_path == fake_bs.vm_info["rootfs_path"]
        assert path == "/workspace/reports/report.md"
        return "fallback text"

    monkeypatch.setattr(vm_module.MicroVM, "get_file_contents", fake_get_file_contents, raising=False)

    resp = client.get(
        f"/api/vms/{fake_bs.vm.vm_id}/read-file",
        params={"path": "/workspace/reports/report.md"},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "path": "/workspace/reports/report.md",
        "content": "fallback text",
    }


def test_file_info_upload_and_http_proxy(client, fake_bs):
    info_resp = client.get(
        f"/api/vms/{fake_bs.vm.vm_id}/file-info",
        params={"path": "/tmp/out.txt"},
    )
    assert info_resp.status_code == 200
    assert info_resp.json()["info"] == {"size": 9}

    upload_resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/upload",
        data={"remote_path": "/tmp/upload.txt"},
        files={"file": ("upload.txt", b"uploaded")},
    )
    assert upload_resp.status_code == 200
    assert fake_bs.vm.uploads[-1] == (b"uploaded", "/tmp/upload.txt", False)

    http_resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/http",
        json={"port": 8080, "path": "/health", "method": "POST"},
    )
    assert http_resp.status_code == 200
    assert http_resp.json()["status_code"] == 201
    assert http_resp.json()["body"] == "POST 8080/health"


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


def test_download_file_falls_back_for_stopped_vm(client, fake_bs, monkeypatch):
    fake_bs.get_vm = lambda vm_id: None

    def fake_download_file(self, remote, local):
        assert self.rootfs_path == fake_bs.vm_info["rootfs_path"]
        assert remote == "/workspace/reports/report.md"
        Path(local).write_bytes(b"fallback bytes")

    monkeypatch.setattr(vm_module.MicroVM, "download_file", fake_download_file, raising=False)

    resp = client.get(
        f"/api/vms/{fake_bs.vm.vm_id}/download",
        params={"path": "/workspace/reports/report.md"},
    )

    assert resp.status_code == 200
    assert resp.content == b"fallback bytes"


def test_write_file_with_append_flag(client, fake_bs):
    """write-file should forward the append flag to the direct text writer."""
    resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/write-file",
        json={"path": "/tmp/log.txt", "content": "line\n", "append": True},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "appended", "path": "/tmp/log.txt"}
    assert fake_bs.vm.text_writes[-1] == ("/tmp/log.txt", "line\n", True)


def test_append_file_endpoint_utf8(client, fake_bs):
    resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/append-file",
        json={"path": "/tmp/log.txt", "content": "more\n"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "appended", "path": "/tmp/log.txt"}
    assert fake_bs.vm.text_writes[-1] == ("/tmp/log.txt", "more\n", True)


def test_append_file_endpoint_base64(client, fake_bs):
    import base64 as _b64

    payload = b"\x00binary\xff"
    resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/append-file",
        json={
            "path": "/tmp/blob.bin",
            "content": _b64.b64encode(payload).decode("ascii"),
            "encoding": "base64",
        },
    )
    assert resp.status_code == 200
    assert fake_bs.vm.byte_writes[-1] == ("/tmp/blob.bin", payload, True)


def test_append_file_endpoint_base64_rejects_oversized_payload(client, fake_bs, monkeypatch):
    import base64 as _b64

    monkeypatch.setattr(server, "_MAX_FILE_WRITE_BYTES", 3)
    monkeypatch.setattr(server, "_MAX_BASE64_FILE_CONTENT_CHARS", 4)

    resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/append-file",
        json={
            "path": "/tmp/blob.bin",
            "content": _b64.b64encode(b"toolong").decode("ascii"),
            "encoding": "base64",
        },
    )

    assert resp.status_code == 413
    assert fake_bs.vm.byte_writes == []


def test_append_file_endpoint_base64_rejects_invalid_payload(client, fake_bs):
    resp = client.post(
        f"/api/vms/{fake_bs.vm.vm_id}/append-file",
        json={
            "path": "/tmp/blob.bin",
            "content": "not base64!",
            "encoding": "base64",
        },
    )

    assert resp.status_code == 400
    assert fake_bs.vm.byte_writes == []


def test_append_file_endpoint_vm_not_found(client):
    resp = client.post(
        "/api/vms/unknown/append-file",
        json={"path": "/tmp/x", "content": "x"},
    )
    assert resp.status_code == 404


def test_terminal_websocket(client, fake_bs):
    with client.websocket_connect(f"/api/vms/{fake_bs.vm.vm_id}/terminal?cols=80&rows=24") as ws:
        message = ws.receive_text()
        assert message == "output"
        ws.send_text(json.dumps({"type": "input", "data": "Zm9v"}))
    assert fake_bs.vm.inputs == [("session-1", "Zm9v", "base64")]
    assert fake_bs.vm.killed_sessions == ["session-1"]


def test_terminal_websocket_accepts_subprotocol_auth(client, fake_bs, tmp_path, monkeypatch):
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    _, api_key, _ = server.init_auth_config(auth_dir)
    monkeypatch.setattr(server, "_auth_storage", auth_dir)
    encoded = base64.urlsafe_b64encode(api_key.encode()).decode().rstrip("=")

    with client.websocket_connect(
        f"/api/vms/{fake_bs.vm.vm_id}/terminal?cols=80&rows=24",
        subprotocols=["bandsox.terminal", f"bandsox.auth.{encoded}"],
    ) as ws:
        assert ws.accepted_subprotocol == "bandsox.terminal"
        assert ws.receive_text() == "output"
