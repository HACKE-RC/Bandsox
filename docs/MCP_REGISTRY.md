# MCP server registry

[`bandsox/mcp_registry.py`](../bandsox/mcp_registry.py) maps short server names like `browserbase` or `github` to the command, args, and env-var conventions Claude Code expects. The `mcp=` argument on `create_vm` reads from this registry; if you want to support a new MCP server, add it here.

## Why this exists

The Claude Code MCP config format is verbose:

```json
{
  "mcpServers": {
    "browserbase": {
      "command": "npx",
      "args": ["-y", "@browserbasehq/mcp"],
      "env": {
        "BROWSERBASE_API_KEY": "...",
        "BROWSERBASE_PROJECT_ID": "..."
      }
    }
  }
}
```

Callers shouldn't have to write that by hand every time. The registry takes a minimal dict:

```python
mcp={"browserbase": {"apiKey": "...", "projectId": "..."}}
```

and BandSox builds the full envelope, sets the right env vars in the VM, and stages the JSON at `/workspace/.mcp.json` inside the rootfs before boot.

## What's in the box

| Name | Command | Default args | User-facing params |
| --- | --- | --- | --- |
| `browserbase` | `npx` | `-y @browserbasehq/mcp` | `apiKey` → `BROWSERBASE_API_KEY`, `projectId` → `BROWSERBASE_PROJECT_ID` |
| `fetch` | `uvx` | `mcp-server-fetch` | none (needs `uvx` in the image) |
| `filesystem` | `npx` | `-y @modelcontextprotocol/server-filesystem /workspace` | `path` overrides the allowed directory |
| `github` | `npx` | `-y @modelcontextprotocol/server-github` | `token` → `GITHUB_PERSONAL_ACCESS_TOKEN` |
| `slack` | `npx` | `-y @modelcontextprotocol/server-slack` | `token` → `SLACK_BOT_TOKEN` (upstream archived) |

## Behaviour worth knowing

### Unknown server names raise

Typos like `"githhub"` produce a `ValueError` instead of silently doing nothing. The error message points at the registry and at the `spec` escape hatch (below) so the caller can pick.

### Secrets are redacted on disk, not at runtime

Every env-var name that appears as a value in some `env_map` is added to `bandsox.mcp_registry.SECRET_ENV_NAMES` at module load. When BandSox writes the VM's `metadata.json`, those values are replaced with `<redacted>`. The live VM still receives the real values through `vm.env_vars`, so MCP servers and Claude Code itself work normally.

### Explicit `env_vars` wins over MCP-derived ones

If the caller passes both `env_vars={"GITHUB_PERSONAL_ACCESS_TOKEN": "explicit"}` and `mcp={"github": {"token": "from-mcp"}}`, the explicit value wins. A warning is logged so the override isn't silent.

### The `/workspace/` parent directory is created on the fly

`debugfs write` doesn't create intermediate directories, so `write_mcp_config_to_rootfs` issues an `mkdir` first. That makes the helper work on custom images that don't have `WORKDIR /workspace` set in the Dockerfile.

## Custom servers (the `spec` escape hatch)

For anything not in the registry, pass a raw spec under the `spec` key:

```python
mcp={
    "my-custom": {
        "spec": {
            "command": "uvx",
            "args": ["my-mcp-package"],
            "env": {"MY_API_TOKEN": "..."},
        }
    }
}
```

Raw mode forwards the spec verbatim to `.mcp.json` and never touches `env_map`. Env vars have to be set inside `spec.env` directly. `command` is required, the rest is optional.

## Adding a server to the registry

Open [`bandsox/mcp_registry.py`](../bandsox/mcp_registry.py) and add an entry under `MCP_SERVERS`:

```python
MCP_SERVERS: dict[str, MCPServerEntry] = {
    # ...
    "linear": {
        "command": "npx",
        "args": ["-y", "@linear/mcp-server"],
        "env_map": {
            "apiKey": "LINEAR_API_KEY",
        },
    },
}
```

That's all the wiring you need. `LINEAR_API_KEY` gets picked up by `SECRET_ENV_NAMES` automatically, and `resolve_mcp_config` will accept `{"linear": {"apiKey": "..."}}` immediately. Add a test in [`tests/test_mcp_registry.py`](../tests/test_mcp_registry.py) following the `browserbase` cases: happy path, partial params, no env block when no params are passed.

If the new server needs CLI args derived from user input (the `filesystem` server's `path` argument is the only existing example), handle it in `resolve_mcp_config` and add a sentinel check so the args layout can't drift silently:

```python
if name == "linear" and "team" in params:
    if len(args) < N or args[K] != EXPECTED_DEFAULT:
        raise RuntimeError("linear MCP_SERVERS args layout changed; ...")
    args[K] = str(params["team"])
```

## Public symbols

```python
from bandsox.mcp_registry import (
    MCP_SERVERS,                 # dict[name, MCPServerEntry]
    MCP_CONFIG_PATH,             # "/workspace/.mcp.json"
    SECRET_ENV_NAMES,            # set[str], seeded from env_map values at import
    resolve_mcp_config,          # user_mcp -> (mcp_servers, env_vars)
    write_mcp_config_to_rootfs,  # (rootfs_path, {"mcpServers": ...}) -> None
    get_server_config,           # name -> MCPServerEntry | None
)
```

`resolve_mcp_config(None)` and `resolve_mcp_config({})` both return `({}, {})`. The redaction helpers (`_redact_secrets`, `_redact_mcp_servers`) live in [`bandsox/core.py`](../bandsox/core.py) instead of here because they're about persistence, not MCP.

## See also

- [docs/CLAUDE_CODE.md](CLAUDE_CODE.md) for the end-to-end Claude Code cookbook.
- [docs/API.md](API.md) for the top-level BandSox API reference.
