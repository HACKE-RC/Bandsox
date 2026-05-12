"""Regression tests for exec stdout/stderr over the vsock output path."""

import base64
import json
import logging
from pathlib import Path

import pytest

import bandsox.vm as vm_module
from bandsox.vm import MicroVM


class FakeVsockListener:
    def __init__(self):
        self.running = True
        self.listener_path = "/tmp/fake-vsock.sock_9000"
        self.registered = {}
        self.all_slots = {}
        self.registered_max_bytes = {}
        self.unregistered = []

    def register_pending_buffer(self, cmd_id, max_bytes=0):
        self.registered_max_bytes[cmd_id] = max_bytes
        slot = {
            "buf": bytearray(),
            "done": FakeEvent(),
            "error": None,
            "max_bytes": max_bytes,
        }
        self.registered[cmd_id] = slot
        self.all_slots[cmd_id] = slot
        return slot

    def unregister_pending_buffer(self, cmd_id):
        self.unregistered.append(cmd_id)
        self.registered.pop(cmd_id, None)


class FakeEvent:
    def __init__(self):
        self._set = False
        self.waits = []

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._set


def test_vsock_exec_output_preserves_text_callback_contract(tmp_path, monkeypatch):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    listener = FakeVsockListener()
    vm.vsock_listener = listener
    vm.vsock_enabled = True
    vm.vsock_port = 9000
    vm.agent_ready = True

    sent_payloads = []

    def fake_send_request_with_id(cmd_id, req_type, payload, **kwargs):
        sent_payloads.append((cmd_id, req_type, payload))
        listener.registered[cmd_id + ":stdout"]["buf"].extend(
            b"hello \xff world\n"
        )
        listener.registered[cmd_id + ":stderr"]["buf"].extend(b"warn\n")
        listener.registered[cmd_id + ":stdout"]["done"].set()
        listener.registered[cmd_id + ":stderr"]["done"].set()
        return 0

    monkeypatch.setattr(vm, "_send_request_with_id", fake_send_request_with_id)

    stdout = []
    stderr = []
    rc = vm.exec_command(
        "printf test",
        on_stdout=stdout.append,
        on_stderr=stderr.append,
    )

    assert rc == 0
    assert stdout == ["hello \ufffd world\n"]
    assert stderr == ["warn\n"]
    assert all(isinstance(chunk, str) for chunk in stdout + stderr)

    cmd_id, req_type, payload = sent_payloads[0]
    assert req_type == "exec"
    assert payload["use_vsock_output"] is True
    assert payload["vsock_port"] == 9000
    assert listener.unregistered == [cmd_id + ":stdout", cmd_id + ":stderr"]


def test_vsock_exec_output_registers_bounded_buffers(tmp_path, monkeypatch):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    listener = FakeVsockListener()
    vm.vsock_listener = listener
    vm.vsock_enabled = True
    vm.vsock_port = 9000
    vm.agent_ready = True

    def fake_send_request_with_id(cmd_id, req_type, payload, **kwargs):
        listener.registered[cmd_id + ":stdout"]["done"].set()
        listener.registered[cmd_id + ":stderr"]["done"].set()
        return 0

    monkeypatch.setattr(vm, "_send_request_with_id", fake_send_request_with_id)

    assert vm.exec_command("true") == 0

    assert len(listener.unregistered) == 2
    assert listener.registered == {}
    for cmd_id in listener.unregistered:
        assert cmd_id.endswith(":stdout") or cmd_id.endswith(":stderr")
        assert listener.registered_max_bytes[cmd_id] == 4 * 1024 * 1024


def test_vsock_exec_requires_configured_vsock_port(tmp_path):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    listener = FakeVsockListener()
    vm.vsock_listener = listener
    vm.vsock_enabled = True
    vm.vsock_port = None
    vm.agent_ready = True

    with pytest.raises(RuntimeError, match="vsock_port is not set"):
        vm.exec_command("true")

    assert listener.registered == {}


def test_vsock_exec_logs_when_output_slot_wait_expires(tmp_path, monkeypatch, caplog):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    listener = FakeVsockListener()
    vm.vsock_listener = listener
    vm.vsock_enabled = True
    vm.vsock_port = 9000
    vm.agent_ready = True

    def fake_send_request_with_id(cmd_id, req_type, payload, **kwargs):
        return 0

    monkeypatch.setattr(vm, "_send_request_with_id", fake_send_request_with_id)
    caplog.set_level(logging.WARNING, logger="bandsox.vm")

    assert vm.exec_command("true") == 0

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "stdout upload" in message
        and "did not finish within 0.6s" in message
        and "bytes may still be pending" in message
        for message in messages
    )
    assert any(
        "stderr upload" in message
        and "did not finish within 0.6s" in message
        and "bytes may still be pending" in message
        for message in messages
    )


def test_vsock_exec_waits_longer_when_agent_confirms_vsock_upload(tmp_path, monkeypatch):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    listener = FakeVsockListener()
    vm.vsock_listener = listener
    vm.vsock_enabled = True
    vm.vsock_port = 9000
    vm.agent_ready = True

    def fake_send_request_with_id(cmd_id, req_type, payload, **kwargs):
        kwargs["exit_metadata"].update(
            {
                "vsock_output": {
                    "requested": True,
                    "attempted": True,
                    "stdout_uploaded": True,
                    "stderr_uploaded": False,
                }
            }
        )
        return 0

    monkeypatch.setattr(vm, "_send_request_with_id", fake_send_request_with_id)

    assert vm.exec_command("true") == 0

    assert listener.all_slots[listener.unregistered[0]]["done"].waits == [3.0]
    assert listener.all_slots[listener.unregistered[1]]["done"].waits == [0.2]


def test_vsock_exec_logs_buffer_upload_errors(tmp_path, monkeypatch, caplog):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    listener = FakeVsockListener()
    vm.vsock_listener = listener
    vm.vsock_enabled = True
    vm.vsock_port = 9000
    vm.agent_ready = True

    def fake_send_request_with_id(cmd_id, req_type, payload, **kwargs):
        slot = listener.registered[cmd_id + ":stdout"]
        slot["error"] = "Upload size 4194305 exceeds buffer cap 4194304"
        slot["done"].set()
        listener.registered[cmd_id + ":stderr"]["done"].set()
        return 0

    monkeypatch.setattr(vm, "_send_request_with_id", fake_send_request_with_id)
    caplog.set_level(logging.WARNING, logger="bandsox.vm")

    assert vm.exec_command("true") == 0

    assert "stdout upload" in caplog.text
    assert "exceeds buffer cap" in caplog.text


def test_send_request_logs_kill_failure_on_timeout(tmp_path, monkeypatch, caplog):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    vm.agent_ready = True

    def fake_write_to_agent(data):
        payload = json.loads(data)
        if payload["type"] == "kill":
            raise RuntimeError("broken pipe")

    monkeypatch.setattr(vm, "_write_to_agent", fake_write_to_agent)
    caplog.set_level(logging.DEBUG, logger="bandsox.vm")

    with pytest.raises(TimeoutError):
        vm._send_request_with_id("cmd-1", "exec", {"command": "sleep 10"}, timeout=0)

    assert "Failed to send kill for timed-out command cmd-1" in caplog.text
    assert "broken pipe" in caplog.text


def test_windowed_get_file_contents_uses_serial_windowing(tmp_path, monkeypatch):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    vm.vsock_listener = FakeVsockListener()
    vm.vsock_enabled = True
    vm.vsock_port = 9000
    vm.agent_ready = True

    sent_payloads = []

    def fake_send_request_with_id(cmd_id, req_type, payload, on_file_content=None, **kwargs):
        sent_payloads.append((req_type, payload))
        assert on_file_content is not None
        on_file_content(base64.b64encode(b"Line 2\n").decode("ascii"))
        return 0

    monkeypatch.setattr(vm, "_send_request_with_id", fake_send_request_with_id)
    monkeypatch.setattr(
        vm,
        "_write_to_agent",
        lambda data: (_ for _ in ()).throw(
            AssertionError("windowed reads must not use full-file vsock upload")
        ),
    )

    content = vm.get_file_contents(
        "/tmp/file.txt",
        offset=1,
        limit=1,
        show_header=False,
        show_footer=False,
    )

    assert content == "Line 2\n"
    assert sent_payloads == [
        (
            "read_file",
            {
                "path": "/tmp/file.txt",
                "offset": 1,
                "limit": 1,
                "show_line_numbers": False,
            },
        )
    ]


def test_windowed_get_file_contents_formats_agent_window_without_full_vsock(tmp_path, monkeypatch):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    listener = FakeVsockListener()
    vm.vsock_listener = listener
    vm.vsock_enabled = True
    vm.vsock_port = 9000
    vm.agent_ready = True

    sent_payloads = []

    def fake_send_request_with_id(cmd_id, req_type, payload, on_file_content=None, **kwargs):
        sent_payloads.append((req_type, payload))
        assert on_file_content is not None
        vm.event_callbacks[cmd_id] = {"_agent_total_lines": 3}
        on_file_content(base64.b64encode(b"Line 2").decode("ascii"))
        return 0

    monkeypatch.setattr(vm, "_send_request_with_id", fake_send_request_with_id)
    monkeypatch.setattr(
        vm,
        "_write_to_agent",
        lambda data: (_ for _ in ()).throw(
            AssertionError("windowed reads with header/footer must not use full-file vsock upload")
        ),
    )

    content = vm.get_file_contents("/tmp/file.txt", offset=1, limit=1)

    assert sent_payloads == [
        (
            "read_file",
            {
                "path": "/tmp/file.txt",
                "offset": 1,
                "limit": 1,
                "show_line_numbers": False,
            },
        )
    ]
    assert "skipped 1 lines" in content
    assert "Line 2" in content
    assert "... 1 lines left" in content


def test_debugfs_windowed_fallback_logs_full_file_formatting(tmp_path, monkeypatch, caplog):
    vm = MicroVM("vm-test", str(tmp_path / "fc.sock"))
    vm.agent_ready = False
    monkeypatch.setattr(vm, "_has_debugfs_rootfs", lambda: True)
    monkeypatch.setattr(vm_module, "_DEBUGFS_FULL_FILE_FALLBACK_LOG_THRESHOLD", 16)

    def fake_debugfs_download(path, temp_path):
        Path(temp_path).write_bytes(b"line 1\nline 2\nline 3\n")

    monkeypatch.setattr(vm, "_debugfs_download_file", fake_debugfs_download)
    caplog.set_level(logging.WARNING, logger="bandsox.vm")

    content = vm.get_file_contents("/tmp/big.txt", offset=1, limit=1)

    assert "line 2" in content
    assert "debugfs fallback read" in caplog.text
