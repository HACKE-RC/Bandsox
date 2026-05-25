"""Registry of known MCP servers + helpers for staging Claude Code's
MCP configuration into a Bandsox VM rootfs.

Add new entries to MCP_SERVERS to extend support. Each entry defines the
process to launch and how user-supplied parameters map to environment variables.

To use a server that is not in the registry, pass an explicit raw spec under
a `spec` key, e.g.:

    mcp={"my-custom": {"spec": {"command": "...", "args": [...], "env": {...}}}}

Raw mode never reads `env_map`; env vars must be set explicitly inside `spec.env`.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import TypedDict

logger = logging.getLogger(__name__)


class MCPServerEntry(TypedDict, total=False):
    command: str
    args: list[str]
    env_map: dict[str, str]


# Claude Code reads project-scope MCP config from $WORKDIR/.mcp.json. Our
# claude-code image uses WORKDIR=/workspace, so we stage there. This is also
# exactly what `claude mcp add` writes when project scope is selected.
MCP_CONFIG_PATH = "/workspace/.mcp.json"

# Env var names whose values originate from MCP credentials and must not be
# persisted to VM metadata files on disk. Kept in sync with env_map values
# below. Callers can extend at runtime.
SECRET_ENV_NAMES: set[str] = set()

MCP_SERVERS: dict[str, MCPServerEntry] = {
    "browserbase": {
        "command": "npx",
        "args": ["-y", "@browserbasehq/mcp"],
        "env_map": {
            "apiKey": "BROWSERBASE_API_KEY",
            "projectId": "BROWSERBASE_PROJECT_ID",
        },
    },
    # The official fetch MCP server is the Python `mcp-server-fetch`, run via uvx.
    # The base image must therefore have `uvx` available; the claude-code image
    # does not yet include it, so users wanting fetch should add it to a custom image.
    "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "env_map": {},
    },
    "filesystem": {
        "command": "npx",
        # args[2] is the directory the server is allowed to touch and is
        # overridable via params["path"] (see resolve_mcp_config).
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
        "env_map": {},
    },
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env_map": {
            "token": "GITHUB_PERSONAL_ACCESS_TOKEN",
        },
    },
    # Note: the official Slack MCP server has been archived upstream; entry kept
    # for parity with E2B's catalog but may stop working without notice.
    "slack": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env_map": {
            "token": "SLACK_BOT_TOKEN",
        },
    },
}

# Seed the secret set from every env_map value in the registry.
for _entry in MCP_SERVERS.values():
    SECRET_ENV_NAMES.update(_entry.get("env_map", {}).values())


def get_server_config(name: str) -> MCPServerEntry | None:
    """Return registry entry for the named server, or None if unknown."""
    return MCP_SERVERS.get(name)


def resolve_mcp_config(
    user_mcp: dict[str, dict] | None,
) -> tuple[dict, dict]:
    """Resolve a user-facing mcp= dict into (mcpServers dict, env vars to inject).

    Two input shapes per server:

    1. Named server from MCP_SERVERS:
           {"browserbase": {"apiKey": "...", "projectId": "..."}}
       Parameters are mapped via env_map and injected as the server's env block.
       For "filesystem", a "path" parameter overrides the allowed directory.

    2. Raw passthrough (explicit sentinel):
           {"my-thing": {"spec": {"command": "...", "args": [...], "env": {...}}}}
       Spec is forwarded verbatim; env_map is not consulted.

    Unknown server names raise ValueError (typos must not silently no-op).
    Returns (mcp_servers_dict, env_vars_dict).
    """
    if not user_mcp:
        return {}, {}

    mcp_servers: dict[str, dict] = {}
    env_vars: dict[str, str] = {}

    for name, params in user_mcp.items():
        if not isinstance(params, dict):
            raise ValueError(
                f"mcp[{name!r}] must be a dict; got {type(params).__name__}"
            )

        if "spec" in params:
            spec = params["spec"]
            if not isinstance(spec, dict) or "command" not in spec:
                raise ValueError(
                    f"mcp[{name!r}].spec must be a dict containing 'command'"
                )
            entry: dict = {"command": spec["command"]}
            if "args" in spec:
                entry["args"] = list(spec["args"])
            if "env" in spec:
                entry["env"] = dict(spec["env"])
            mcp_servers[name] = entry
            continue

        registry_entry = get_server_config(name)
        if not registry_entry:
            raise ValueError(
                f"Unknown MCP server: {name!r}. "
                f"Pass {{'spec': {{...}}}} for custom servers, or add it to "
                f"bandsox.mcp_registry.MCP_SERVERS."
            )

        # Per-server arg customization. Currently only "filesystem" supports
        # this; generalise if more servers grow tunable args.
        args = list(registry_entry.get("args", []))
        if name == "filesystem" and "path" in params:
            # Guard against the registry's default args drifting: the slot we
            # rewrite must currently hold the documented default directory.
            # If anyone reorders or adds flags, this assert catches it before
            # we silently scribble over the wrong arg.
            if len(args) < 3 or args[2] != "/workspace":
                raise RuntimeError(
                    "filesystem MCP_SERVERS args layout changed; update the "
                    "path-override slot in resolve_mcp_config."
                )
            args[2] = str(params["path"])

        server_entry: dict = {
            "command": registry_entry["command"],
            "args": args,
        }
        server_env: dict[str, str] = {}
        for user_key, env_name in registry_entry.get("env_map", {}).items():
            if user_key in params:
                val = str(params[user_key])
                server_env[env_name] = val
                env_vars[env_name] = val
        if server_env:
            server_entry["env"] = server_env

        mcp_servers[name] = server_entry

    return mcp_servers, env_vars


def write_mcp_config_to_rootfs(rootfs_path: str, mcp_config: dict) -> None:
    """Write a Claude Code MCP config JSON into the given ext4 rootfs image.

    `mcp_config` is the full envelope, e.g. {"mcpServers": {...}}. The file
    lands at MCP_CONFIG_PATH inside the image (project-scope). The claude-code
    template ships `WORKDIR /workspace` so the parent dir already exists, but
    we also issue `mkdir` defensively for custom images that don't.

    Uses debugfs to avoid requiring loopback mounts. Raises on failure --
    silently shipping a VM with no MCP wired in would be worse.
    """
    if not mcp_config:
        return

    if shutil.which("debugfs") is None:
        raise RuntimeError(
            "debugfs not found on PATH. Install e2fsprogs (provides debugfs) "
            "to enable MCP config injection."
        )

    parent = os.path.dirname(MCP_CONFIG_PATH) or "/"

    content = json.dumps(mcp_config, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(prefix="bandsox-mcp-", suffix=".json")
    try:
        os.write(fd, content)
        os.close(fd)

        # debugfs runs each line independently; a failing line (e.g. mkdir
        # of an existing dir, rm of a missing file) just prints to stderr
        # and the script continues. The only line that MUST succeed is
        # `write`, and we assert on that via a final stat. The non-zero
        # exit from debugfs itself reflects the LAST command's status, so
        # we end with `stat` to ensure success surfaces clearly.
        script = (
            f"mkdir {parent}\n"
            f"rm {MCP_CONFIG_PATH}\n"
            f"write {tmp_path} {MCP_CONFIG_PATH}\n"
            f"stat {MCP_CONFIG_PATH}\n"
        )
        proc = subprocess.run(
            ["debugfs", "-w", "-f", "/dev/stdin", rootfs_path],
            input=script,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        # debugfs returns 0 if the LAST command succeeded; if stat() fails
        # the file was not written. Check both exit code and stat output.
        stat_ok = "Inode:" in (proc.stdout or "")
        if proc.returncode != 0 or not stat_ok:
            raise RuntimeError(
                f"debugfs failed to write MCP config to {rootfs_path}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
