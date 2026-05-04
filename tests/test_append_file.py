"""Tests for the append-file pathway across agent, SDK client, and guest VM helper."""

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------- Guest agent: handle_write_file ----------

def test_agent_handle_write_file_creates_then_appends(tmp_path, monkeypatch):
    """handle_write_file with append=False truncates; with append=True extends."""
    from bandsox import agent as agent_mod

    events = []
    monkeypatch.setattr(agent_mod, "send_event", lambda t, p: events.append((t, p)))

    target = tmp_path / "out.log"

    agent_mod.handle_write_file(
        cmd_id="c1", path=str(target), content=base64.b64encode(b"hello").decode(), append=False
    )
    agent_mod.handle_write_file(
        cmd_id="c2", path=str(target), content=base64.b64encode(b" world").decode(), append=True
    )

    assert target.read_bytes() == b"hello world"
    # Second call must NOT have truncated.
    exits = [p["exit_code"] for t, p in events if t == "exit"]
    assert exits == [0, 0]


def test_agent_handle_write_file_append_creates_when_missing(tmp_path, monkeypatch):
    """append=True against a non-existent file should create it (open(..., 'ab'))."""
    from bandsox import agent as agent_mod

    monkeypatch.setattr(agent_mod, "send_event", lambda t, p: None)

    target = tmp_path / "fresh.log"
    agent_mod.handle_write_file(
        cmd_id="c1", path=str(target), content=base64.b64encode(b"abc").decode(), append=True
    )
    assert target.read_bytes() == b"abc"


def test_agent_handle_write_file_creates_parent_dirs(tmp_path, monkeypatch):
    from bandsox import agent as agent_mod

    monkeypatch.setattr(agent_mod, "send_event", lambda t, p: None)

    target = tmp_path / "nested" / "deep" / "f.log"
    agent_mod.handle_write_file(
        cmd_id="c1", path=str(target), content=base64.b64encode(b"x").decode(), append=True
    )
    assert target.read_bytes() == b"x"


# ---------- Guest VM SDK: upload_file append flag ----------

def _make_vm_with_recorder():
    from bandsox import vm as vm_mod

    sent = []

    class StubVM(vm_mod.MicroVM):
        def __init__(self):
            # Skip MicroVM.__init__ — we only exercise upload_file.
            self.agent_ready = True

        def send_request(self, op, payload, timeout=None):
            sent.append((op, payload, timeout))
            return {"exit_code": 0}

    return StubVM(), sent


def test_vm_upload_file_small_respects_append(tmp_path):
    vm, sent = _make_vm_with_recorder()
    local = tmp_path / "small.txt"
    local.write_bytes(b"tiny")

    vm.upload_file(str(local), "/remote/small.txt", append=True)

    assert len(sent) == 1
    op, payload, _ = sent[0]
    assert op == "write_file"
    assert payload["path"] == "/remote/small.txt"
    assert payload["append"] is True
    assert base64.b64decode(payload["content"]) == b"tiny"


def test_vm_upload_file_small_default_overwrites(tmp_path):
    vm, sent = _make_vm_with_recorder()
    local = tmp_path / "small.txt"
    local.write_bytes(b"tiny")

    vm.upload_file(str(local), "/remote/small.txt")

    assert sent[0][1]["append"] is False


def test_vm_upload_file_chunked_first_chunk_honors_append(tmp_path):
    """When append=True, the FIRST chunk must also append (not truncate)."""
    vm, sent = _make_vm_with_recorder()
    local = tmp_path / "big.bin"
    payload = b"A" * (5 * 1024)  # 5KB > 2KB chunk size
    local.write_bytes(payload)

    vm.upload_file(str(local), "/remote/big.bin", append=True)

    assert len(sent) >= 3  # 5KB / 2KB chunks => 3 chunks
    # All chunks must have append=True so we never truncate the destination.
    appends = [p["append"] for _, p, _ in sent]
    assert appends == [True] * len(sent)

    # And the concatenated payload matches.
    reconstructed = b"".join(base64.b64decode(p["content"]) for _, p, _ in sent)
    assert reconstructed == payload


def test_vm_upload_file_chunked_default_first_chunk_truncates(tmp_path):
    """Default behavior: first chunk overwrites, subsequent chunks append."""
    vm, sent = _make_vm_with_recorder()
    local = tmp_path / "big.bin"
    local.write_bytes(b"B" * (5 * 1024))

    vm.upload_file(str(local), "/remote/big.bin")

    appends = [p["append"] for _, p, _ in sent]
    assert appends[0] is False
    assert all(a is True for a in appends[1:])


# ---------- Client SDK (RemoteMicroVM): append_text / append_file ----------

def _make_remote_vm():
    from bandsox import core

    bs = MagicMock()
    bs.timeout = 30
    bs._request = MagicMock(return_value={"status": "appended"})
    return core.RemoteMicroVM("vm-test", bs), bs


def test_remote_vm_append_text_posts_to_append_endpoint():
    vm, bs = _make_remote_vm()
    vm.append_text("/tmp/out.log", "hello world")

    bs._request.assert_called_once()
    args, kwargs = bs._request.call_args
    assert args[0] == "POST"
    assert args[1] == "/api/vms/vm-test/append-file"
    assert kwargs["json"] == {
        "path": "/tmp/out.log",
        "content": "hello world",
        "encoding": "utf-8",
        "append": True,
    }


def test_remote_vm_append_file_posts_base64(tmp_path):
    vm, bs = _make_remote_vm()
    local = tmp_path / "blob.bin"
    local.write_bytes(b"\x00\x01\x02 binary")

    vm.append_file(str(local), "/tmp/blob.bin")

    bs._request.assert_called_once()
    args, kwargs = bs._request.call_args
    assert args[1] == "/api/vms/vm-test/append-file"
    body = kwargs["json"]
    assert body["path"] == "/tmp/blob.bin"
    assert body["encoding"] == "base64"
    assert body["append"] is True
    assert base64.b64decode(body["content"]) == b"\x00\x01\x02 binary"


def test_remote_vm_append_file_missing_raises(tmp_path):
    vm, _ = _make_remote_vm()
    with pytest.raises(FileNotFoundError):
        vm.append_file(str(tmp_path / "does_not_exist.bin"), "/tmp/x")
