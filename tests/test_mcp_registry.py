"""Tests for bandsox.mcp_registry (resolve + write helpers) and the
core.py redaction helpers that depend on it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandsox import mcp_registry
from bandsox.core import _redact_mcp_servers, _redact_secrets, _REDACTED
from bandsox.mcp_registry import (
    MCP_CONFIG_PATH,
    MCP_SERVERS,
    SECRET_ENV_NAMES,
    resolve_mcp_config,
    write_mcp_config_to_rootfs,
)


# ─── resolve_mcp_config ────────────────────────────────────────────────────


def test_none_and_empty_inputs():
    assert resolve_mcp_config(None) == ({}, {})
    assert resolve_mcp_config({}) == ({}, {})


def test_named_server_browserbase_maps_env():
    servers, env = resolve_mcp_config(
        {"browserbase": {"apiKey": "k", "projectId": "p"}}
    )
    assert servers == {
        "browserbase": {
            "command": "npx",
            "args": ["-y", "@browserbasehq/mcp"],
            "env": {
                "BROWSERBASE_API_KEY": "k",
                "BROWSERBASE_PROJECT_ID": "p",
            },
        }
    }
    assert env == {"BROWSERBASE_API_KEY": "k", "BROWSERBASE_PROJECT_ID": "p"}


def test_named_server_with_missing_optional_keys_omits_env_block():
    servers, env = resolve_mcp_config({"fetch": {}})
    assert servers == {
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}
    }
    # No env_map for fetch, so no env vars are derived.
    assert env == {}


def test_filesystem_path_override():
    servers, _ = resolve_mcp_config({"filesystem": {"path": "/mnt/data"}})
    assert servers["filesystem"]["args"][2] == "/mnt/data"


def test_filesystem_default_path():
    servers, _ = resolve_mcp_config({"filesystem": {}})
    assert servers["filesystem"]["args"][2] == "/workspace"


def test_filesystem_default_args_unmutated_by_override():
    # Guards against a regression where args list is shared across calls.
    resolve_mcp_config({"filesystem": {"path": "/a"}})
    resolve_mcp_config({"filesystem": {"path": "/b"}})
    # Registry default must be untouched.
    assert MCP_SERVERS["filesystem"]["args"][2] == "/workspace"


def test_spec_passthrough():
    servers, env = resolve_mcp_config(
        {
            "custom": {
                "spec": {
                    "command": "uvx",
                    "args": ["my-mcp"],
                    "env": {"FOO": "bar"},
                }
            }
        }
    )
    assert servers == {
        "custom": {
            "command": "uvx",
            "args": ["my-mcp"],
            "env": {"FOO": "bar"},
        }
    }
    # spec mode never contributes to env_vars.
    assert env == {}


def test_spec_minimal_command_only():
    servers, env = resolve_mcp_config({"x": {"spec": {"command": "myserver"}}})
    assert servers == {"x": {"command": "myserver"}}
    assert env == {}


def test_unknown_server_raises():
    with pytest.raises(ValueError, match="Unknown MCP server"):
        resolve_mcp_config({"githhub": {"token": "x"}})


def test_spec_without_command_raises():
    with pytest.raises(ValueError, match="must be a dict containing 'command'"):
        resolve_mcp_config({"x": {"spec": {"args": []}}})


def test_spec_non_dict_raises():
    with pytest.raises(ValueError, match="must be a dict containing 'command'"):
        resolve_mcp_config({"x": {"spec": "oops"}})


def test_non_dict_params_raises():
    with pytest.raises(ValueError, match="must be a dict"):
        resolve_mcp_config({"x": "oops"})


def test_multiple_servers_aggregate_env_correctly():
    servers, env = resolve_mcp_config(
        {
            "github": {"token": "ght"},
            "slack": {"token": "slt"},
        }
    )
    assert set(servers) == {"github", "slack"}
    assert env == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "ght",
        "SLACK_BOT_TOKEN": "slt",
    }


def test_partial_params_omit_unmapped_env_keys():
    # Only one of two mapped keys provided -> env block has only the provided one.
    servers, env = resolve_mcp_config({"browserbase": {"apiKey": "only"}})
    assert servers["browserbase"]["env"] == {"BROWSERBASE_API_KEY": "only"}
    assert env == {"BROWSERBASE_API_KEY": "only"}


def test_no_params_no_env_block():
    servers, _ = resolve_mcp_config({"github": {}})
    assert "env" not in servers["github"]


# ─── secret seeding ────────────────────────────────────────────────────────


def test_secret_env_names_include_all_registry_secrets():
    expected = {
        "BROWSERBASE_API_KEY",
        "BROWSERBASE_PROJECT_ID",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "SLACK_BOT_TOKEN",
    }
    assert expected.issubset(SECRET_ENV_NAMES)


def test_mcp_config_path_is_project_scope():
    assert MCP_CONFIG_PATH == "/workspace/.mcp.json"


# ─── redaction helpers ─────────────────────────────────────────────────────


def test_redact_secrets_strips_known_secrets_keeps_others():
    out = _redact_secrets(
        {
            "ANTHROPIC_API_KEY": "keep",
            "BROWSERBASE_API_KEY": "hide",
            "PATH": "/x",
        }
    )
    assert out == {
        "ANTHROPIC_API_KEY": "keep",
        "BROWSERBASE_API_KEY": _REDACTED,
        "PATH": "/x",
    }


def test_redact_secrets_none_and_empty():
    assert _redact_secrets(None) is None
    assert _redact_secrets({}) == {}


def test_redact_secrets_returns_new_dict():
    src = {"BROWSERBASE_API_KEY": "x"}
    out = _redact_secrets(src)
    assert src == {"BROWSERBASE_API_KEY": "x"}
    assert out is not src


def test_redact_mcp_servers_strips_env_blocks():
    out = _redact_mcp_servers(
        {
            "browserbase": {
                "command": "npx",
                "args": ["x"],
                "env": {"BROWSERBASE_API_KEY": "k"},
            }
        }
    )
    assert out == {
        "mcpServers": {
            "browserbase": {
                "command": "npx",
                "args": ["x"],
                "env": {"BROWSERBASE_API_KEY": _REDACTED},
            }
        }
    }


def test_redact_mcp_servers_no_env_unchanged():
    out = _redact_mcp_servers({"x": {"command": "y", "args": []}})
    assert out == {"mcpServers": {"x": {"command": "y", "args": []}}}


def test_redact_mcp_servers_empty_returns_empty_envelope():
    # Empty input is the caller's job to guard; the helper just wraps + redacts.
    assert _redact_mcp_servers({}) == {"mcpServers": {}}


# ─── write_mcp_config_to_rootfs ────────────────────────────────────────────


def test_write_noop_on_empty_config(tmp_path):
    rootfs = tmp_path / "fake.ext4"
    rootfs.write_bytes(b"")
    # Should not raise, should not invoke debugfs.
    write_mcp_config_to_rootfs(str(rootfs), {})


def test_write_raises_when_debugfs_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: None)
    rootfs = tmp_path / "x.ext4"
    rootfs.write_bytes(b"")
    with pytest.raises(RuntimeError, match="debugfs not found"):
        write_mcp_config_to_rootfs(str(rootfs), {"mcpServers": {"a": {"command": "x"}}})


def test_write_raises_when_debugfs_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/debugfs")
    fake_proc = subprocess.CompletedProcess(
        args=["debugfs"],
        returncode=1,
        stdout="",
        stderr="boom",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)
    rootfs = tmp_path / "x.ext4"
    rootfs.write_bytes(b"")
    with pytest.raises(RuntimeError, match="debugfs failed"):
        write_mcp_config_to_rootfs(str(rootfs), {"mcpServers": {"a": {"command": "x"}}})


def test_write_succeeds_when_parent_dir_does_not_exist(tmp_path):
    """Real ext4 test: parent /workspace not pre-created. mkdir in script handles it."""
    rootfs = tmp_path / "fresh.ext4"
    subprocess.run(["truncate", "-s", "8M", str(rootfs)], check=True)
    subprocess.run(
        ["mkfs.ext4", "-q", "-F", "-O", "^metadata_csum,^64bit", str(rootfs)],
        check=True,
    )
    # Intentionally do NOT mkdir /workspace -- this is the regression we are
    # pinning. (The audit's P1 #3.)
    write_mcp_config_to_rootfs(
        str(rootfs), {"mcpServers": {"a": {"command": "x"}}}
    )
    # Read it back via debugfs to confirm.
    out = tmp_path / "out.json"
    subprocess.run(
        ["debugfs", "-R", f'dump -p "{MCP_CONFIG_PATH}" "{out}"', str(rootfs)],
        check=True,
        capture_output=True,
    )
    assert json.loads(out.read_bytes()) == {"mcpServers": {"a": {"command": "x"}}}


def test_write_succeeds_when_parent_dir_already_exists(tmp_path):
    """Same but with the parent dir pre-created -- mkdir of existing dir must not break."""
    rootfs = tmp_path / "fresh.ext4"
    subprocess.run(["truncate", "-s", "8M", str(rootfs)], check=True)
    subprocess.run(
        ["mkfs.ext4", "-q", "-F", "-O", "^metadata_csum,^64bit", str(rootfs)],
        check=True,
    )
    parent = os.path.dirname(MCP_CONFIG_PATH)
    subprocess.run(
        ["debugfs", "-w", "-R", f"mkdir {parent}", str(rootfs)],
        check=True,
        capture_output=True,
    )
    write_mcp_config_to_rootfs(
        str(rootfs), {"mcpServers": {"a": {"command": "x"}}}
    )
    out = tmp_path / "out.json"
    subprocess.run(
        ["debugfs", "-R", f'dump -p "{MCP_CONFIG_PATH}" "{out}"', str(rootfs)],
        check=True,
        capture_output=True,
    )
    assert json.loads(out.read_bytes()) == {"mcpServers": {"a": {"command": "x"}}}


def _extract_write_source(script: str) -> str:
    """Pull the temp-file path out of the `write <tmp> <dst>` line in a debugfs script."""
    for line in script.splitlines():
        parts = line.split()
        if parts[:1] == ["write"] and len(parts) == 3:
            return parts[1]
    raise AssertionError(f"no `write` line found in script: {script!r}")


def test_write_invokes_debugfs_with_mkdir_rm_write_stat_script(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/debugfs")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        # Verify the staged temp file actually exists and has our JSON in it
        # at the moment debugfs is invoked.
        tmp_in_script = _extract_write_source(kwargs["input"])
        captured["tmp_contents"] = Path(tmp_in_script).read_bytes()
        return subprocess.CompletedProcess(cmd, 0, "Inode: 12 Type: regular", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rootfs = tmp_path / "x.ext4"
    rootfs.write_bytes(b"")
    cfg = {"mcpServers": {"a": {"command": "npx", "args": ["-y", "p"]}}}
    write_mcp_config_to_rootfs(str(rootfs), cfg)

    assert captured["cmd"][:4] == ["debugfs", "-w", "-f", "/dev/stdin"]
    assert captured["cmd"][4] == str(rootfs)
    # Script does mkdir-rm-write-stat at the configured path.
    lines = captured["input"].strip().splitlines()
    parent = os.path.dirname(MCP_CONFIG_PATH)
    assert lines[0] == f"mkdir {parent}"
    assert lines[1] == f"rm {MCP_CONFIG_PATH}"
    assert lines[2].startswith("write ") and lines[2].endswith(f" {MCP_CONFIG_PATH}")
    assert lines[3] == f"stat {MCP_CONFIG_PATH}"
    # JSON content matches.
    assert json.loads(captured["tmp_contents"]) == cfg


def test_write_raises_when_stat_missing_from_output(monkeypatch, tmp_path):
    """Even with returncode 0, if the write didn't land, surface that as an error."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/debugfs")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "(no inode info)", ""),
    )
    rootfs = tmp_path / "x.ext4"
    rootfs.write_bytes(b"")
    with pytest.raises(RuntimeError, match="debugfs failed"):
        write_mcp_config_to_rootfs(str(rootfs), {"mcpServers": {"a": {"command": "x"}}})


def test_write_cleans_up_temp_file(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/debugfs")
    seen_tmp: list[str] = []

    def fake_run(cmd, **kwargs):
        tmp_in_script = _extract_write_source(kwargs["input"])
        seen_tmp.append(tmp_in_script)
        return subprocess.CompletedProcess(cmd, 0, "Inode: 1", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rootfs = tmp_path / "x.ext4"
    rootfs.write_bytes(b"")
    write_mcp_config_to_rootfs(str(rootfs), {"mcpServers": {"a": {"command": "x"}}})
    assert seen_tmp and not os.path.exists(seen_tmp[0])
