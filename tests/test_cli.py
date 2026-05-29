import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox import cli


class DummyResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["bandsox"] + args)
    cli.main()


def test_serve_sets_storage_and_runs_uvicorn(monkeypatch, capsys):
    called = {}

    def fake_run(app, host, port, reload):
        called.update({"app": app, "host": host, "port": port, "reload": reload})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setenv("BANDSOX_STORAGE", "unset")

    run_cli(monkeypatch, ["serve", "--port", "9000", "--host", "127.0.0.1", "--storage", "/tmp/data"])
    out = capsys.readouterr().out

    assert os.environ["BANDSOX_STORAGE"] == "/tmp/data"
    assert called == {
        "app": "bandsox.server:app",
        "host": "127.0.0.1",
        "port": 9000,
        "reload": False,
    }
    assert "Setting storage path to /tmp/data" in out
    assert "Starting dashboard at http://127.0.0.1:9000" in out


def test_serve_defaults_storage(monkeypatch):
    called = {}

    def fake_run(app, host, port, reload):
        called.update({"app": app, "host": host, "port": port, "reload": reload})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.delenv("BANDSOX_STORAGE", raising=False)

    run_cli(monkeypatch, ["serve"])

    assert os.environ["BANDSOX_STORAGE"] == "/var/lib/sandbox"
    assert called["port"] == 8000
    assert called["host"] == "0.0.0.0"


def test_serve_reload_flag(monkeypatch):
    called = {}

    def fake_run(app, host, port, reload):
        called.update({"reload": reload})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setenv("BANDSOX_STORAGE", "unset")

    run_cli(monkeypatch, ["serve", "--reload"])

    assert called["reload"] is True


def test_terminal_invokes_client(monkeypatch):
    called = []

    def fake_terminal(vm_id, host, port):
        called.append((vm_id, host, port))

    monkeypatch.setattr(cli, "terminal_client", fake_terminal)
    # terminal now resolves the identifier via the API first; return a VM whose
    # id matches so resolution short-circuits without a real network call.
    monkeypatch.setattr(
        cli.requests, "get",
        lambda url, **kwargs: DummyResponse(200, [{"id": "vm-1", "name": "vm-1"}]),
    )
    run_cli(monkeypatch, ["terminal", "vm-1", "--host", "example.com", "--port", "9001"])

    assert called == [("vm-1", "example.com", 9001)]


def test_create_vm_success(monkeypatch, capsys):
    captured = {}

    def fake_post(url, json, **kwargs):
        captured.update({"url": url, "json": json})
        return DummyResponse(200, {"id": "vm-xyz"})

    monkeypatch.setattr(cli.requests, "post", fake_post)
    run_cli(monkeypatch, ["create", "alpine", "--name", "demo", "--vcpu", "2", "--mem", "256", "--disk-size", "1024"])
    out = capsys.readouterr().out

    assert captured["url"] == "http://127.0.0.1:8000/api/vms"
    assert captured["json"] == {
        "image": "alpine",
        "name": "demo",
        "vcpu": 2,
        "mem_mib": 256,
        "disk_size_mib": 1024,
    }
    assert "VM created: vm-xyz" in out


def test_create_vm_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli.requests, "post", lambda url, json, **kwargs: DummyResponse(500, text="bad"))

    run_cli(monkeypatch, ["create", "alpine"])
    out = capsys.readouterr().out

    assert "Failed to create VM: bad" in out


def test_vm_list_prints_table(monkeypatch, capsys):
    payload = [
        {"name": "vm-a", "status": "running", "id": "id-a", "image": "img-a"},
        {"name": "vm-b", "status": "stopped", "id": "id-b", "image": "img-b"},
    ]
    monkeypatch.setattr(cli.requests, "get", lambda url, **kwargs: DummyResponse(200, payload))
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 20)))

    run_cli(monkeypatch, ["vm", "list"])
    out = capsys.readouterr().out

    header = out.splitlines()[0]
    assert "Name" in header and "Status" in header and "ID" in header and "Image" in header
    assert "vm-a" in out and "vm-b" in out
    assert "Total: 2 VM(s)" in out


@pytest.mark.parametrize(
    ("args", "expected_url", "expected_message"),
    [
        (["vm", "stop", "vm-1"], "http://127.0.0.1:8000/api/vms/vm-1/stop", "VM vm-1 stopped."),
        (["vm", "pause", "vm-2"], "http://127.0.0.1:8000/api/vms/vm-2/pause", "VM vm-2 paused."),
        (["vm", "resume", "vm-3"], "http://127.0.0.1:8000/api/vms/vm-3/resume", "VM vm-3 resumed."),
    ],
)
def test_vm_lifecycle_posts(monkeypatch, capsys, args, expected_url, expected_message):
    captured = {}
    def record_post(url, **kwargs):
        captured["url"] = url
        return DummyResponse(200)

    monkeypatch.setattr(cli.requests, "post", record_post)

    run_cli(monkeypatch, args)
    out = capsys.readouterr().out

    assert captured["url"] == expected_url
    assert expected_message in out


def test_vm_delete(monkeypatch, capsys):
    captured = {}
    def record_delete(url, **kwargs):
        captured["url"] = url
        return DummyResponse(200)

    monkeypatch.setattr(cli.requests, "delete", record_delete)

    run_cli(monkeypatch, ["vm", "delete", "vm-9"])
    out = capsys.readouterr().out

    assert captured["url"] == "http://127.0.0.1:8000/api/vms/vm-9"
    assert "VM vm-9 deleted." in out


def test_vm_save_snapshot(monkeypatch, capsys):
    captured = {}

    def fake_post(url, json, **kwargs):
        captured.update({"url": url, "json": json})
        return DummyResponse(200, {"snapshot_id": "snap-123"})

    monkeypatch.setattr(cli.requests, "post", fake_post)
    run_cli(monkeypatch, ["vm", "save", "vm-8", "named"])
    out = capsys.readouterr().out

    assert captured["url"] == "http://127.0.0.1:8000/api/vms/vm-8/snapshot"
    assert captured["json"] == {"name": "named"}
    assert "Snapshot created: snap-123 (name=named)" in out


def test_snapshot_list_prints_table(monkeypatch, capsys):
    payload = [
        {"name": "snap-a", "id": "id-a", "status": "ready"},
        {"snapshot_name": "snap-b", "id": "id-b", "status": "pending"},
    ]
    monkeypatch.setattr(cli.requests, "get", lambda url, **kwargs: DummyResponse(200, payload))
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((60, 20)))

    run_cli(monkeypatch, ["snapshot", "list"])
    out = capsys.readouterr().out

    header = out.splitlines()[0]
    assert "Name" in header and "ID" in header and "Status" in header
    assert "snap-a" in out and "snap-b" in out
    assert "Total: 2 snapshot(s)" in out


def test_snapshot_delete(monkeypatch, capsys):
    captured = {}
    def record_delete(url, **kwargs):
        captured["url"] = url
        return DummyResponse(200)

    monkeypatch.setattr(cli.requests, "delete", record_delete)

    run_cli(monkeypatch, ["snapshot", "delete", "snap-9"])
    out = capsys.readouterr().out

    assert captured["url"] == "http://127.0.0.1:8000/api/snapshots/snap-9"
    assert "Snapshot snap-9 deleted." in out


def test_snapshot_restore(monkeypatch, capsys):
    captured = {}

    def fake_post(url, json, **kwargs):
        captured.update({"url": url, "json": json})
        return DummyResponse(200, {"id": "vm-restored"})

    monkeypatch.setattr(cli.requests, "post", fake_post)
    run_cli(monkeypatch, ["snapshot", "restore", "snap-1", "--name", "restored", "--enable-networking"])
    out = capsys.readouterr().out

    assert captured["url"] == "http://127.0.0.1:8000/api/snapshots/snap-1/restore"
    assert captured["json"] == {"name": "restored", "enable_networking": True}
    assert "Snapshot restored to VM: vm-restored" in out


def test_cleanup_deletes_taps(monkeypatch, capsys):
    fake_subprocess = types.SimpleNamespace(calls=[])

    def fake_run(args, capture_output=False, text=False):
        fake_subprocess.calls.append(args)
        if args[:3] == ["ip", "link", "show"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout="1: tapabc: <BROADCAST>\n2: tapxyz: <BROADCAST>\n3: eth0: <BROADCAST>\n",
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    fake_subprocess.run = fake_run
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)

    run_cli(monkeypatch, ["cleanup"])
    out = capsys.readouterr().out

    assert ["ip", "link", "show"] in fake_subprocess.calls
    assert ["sudo", "ip", "link", "delete", "tapabc"] in fake_subprocess.calls
    assert ["sudo", "ip", "link", "delete", "tapxyz"] in fake_subprocess.calls
    assert "Cleaning up stale TAP devices" in out
    assert "Cleaned up 2 devices" in out


def test_init_downloads_artifacts(monkeypatch):
    calls = {}

    def fake_kernel(output_path, kernel_url, force=False):
        calls["kernel"] = (output_path, kernel_url, force)
        return True

    def fake_cni(url, dest_dir, force=False):
        calls["cni"] = (url, dest_dir, force)
        return True

    def fake_rootfs(url, output_path, force=False):
        calls["rootfs"] = (url, output_path, force)
        return True

    monkeypatch.setattr(cli, "download_kernel", fake_kernel)
    monkeypatch.setattr(cli, "download_cni_plugins", fake_cni)
    monkeypatch.setattr(cli, "download_rootfs", fake_rootfs)

    run_cli(
        monkeypatch,
        [
            "init",
            "--kernel-url",
            "k-url",
            "--kernel-output",
            "k-out",
            "--cni-url",
            "c-url",
            "--cni-dir",
            "c-dir",
            "--rootfs-url",
            "r-url",
            "--rootfs-output",
            "r-out",
            "--force",
        ],
    )

    assert calls["kernel"] == ("k-out", "k-url", True)
    assert calls["cni"] == ("c-url", "c-dir", True)
    assert calls["rootfs"] == ("r-url", "r-out", True)


def test_init_skip_flags(monkeypatch, capsys):
    calls = {}

    def fail(*args, **kwargs):
        calls["called"] = True
        raise AssertionError("Should not be called when skipping")

    monkeypatch.setattr(cli, "download_kernel", fail)
    monkeypatch.setattr(cli, "download_cni_plugins", fail)
    monkeypatch.setattr(cli, "download_rootfs", fail)

    run_cli(monkeypatch, ["init", "--skip-kernel", "--skip-cni", "--skip-rootfs"])
    out = capsys.readouterr().out

    assert "Skipping kernel download." in out
    assert "Skipping CNI plugins download." in out
    assert "Skipping rootfs download." in out
    assert "called" not in calls

