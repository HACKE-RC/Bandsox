# MCP server registry

[`bandsox/mcp_registry.py`](../bandsox/mcp_registry.py) maps short, user-friendly server names to their launch parameters and to the env-var conventions Claude Code (and other MCP clients) expect. It is the extensibility surface for the `mcp=` argument on `create_vm`.

## Why this exists

The Claude Code MCP config format (`{"mcpServers": {...}}`) is verbose:

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

You don't want every caller writing that by hand. The registry lets them pass the minimal user-facing dict:

```python
mcp={"browserbase": {"apiKey": "...", "projectId": "..."}}
```

and have BandSox produce the full envelope, set the right env vars in the VM, and stage the JSON inside the rootfs at `/workspace/.mcp.json`.

## What's in the box

| Name | Command | Default args | User-facing params |
| --- | --- | --- | --- |
| `browserbase` | `npx` | `-y @browserbasehq/mcp` | `apiKey` → `BROWSERBASE_API_KEY`, `projectId` → `BROWSERBASE_PROJECT_ID` |
| `fetch` | `uvx` | `mcp-server-fetch` | none (needs `uvx` in the image) |
| `filesystem` | `npx` | `-y @modelcontextprotocol/server-filesystem /workspace` | `path` overrides the allowed directory |
| `github` | `npx` | `-y @modelcontextprotocol/server-github` | `token` → `GITHUB_PERSONAL_ACCESS_TOKEN` |
| `slack` | `npx` | `-y @modelcontextprotocol/server-slack` | `token` → `SLACK_BOT_TOKEN` (upstream archived) |

## Behaviour

- **Unknown server names raise `ValueError`.** Typos like `"githhub"` should fail loudly rather than silently no-op. The error message points at the registry and the `spec` escape hatch.
- **Secret redaction.** Env-var names that appear as values in any `env_map` are added to `bandsox.mcp_registry.SECRET_ENV_NAMES`. The persisted `metadata.json` replaces those values with `<redacted>` while the live VM still gets the unredacted values via `vm.env_vars`.
- **Caller env wins.** If the caller passes `env_vars={"GITHUB_PERSONAL_ACCESS_TOKEN": "explicit"}` *and* `mcp={"github": {"token": "from-mcp"}}`, the explicit value wins and a warning is logged.
- **The `/workspace/.mcp.json` parent directory is created defensively** via `debugfs mkdir` so custom images without `WORKDIR /workspace` still work.

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

Raw mode forwards the spec verbatim to `.mcp.json`. It never consults `env_map`, so env vars must be set explicitly inside `spec.env`. The `command` key is required; everything else is optional.

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

That's it -- the secret set is auto-seeded from `env_map` values at module load, and `resolve_mcp_config` picks the entry up immediately. Add a test in [`tests/test_mcp_registry.py`](../tests/test_mcp_registry.py) mirroring the `browserbase` cases (happy path, partial params, no env block when no params).

If your server needs positional CLI args derived from user input (the `filesystem` server's `path` is the only current case), do it in `resolve_mcp_config` and add a sentinel assert so the args layout can't silently drift:

```python
if name == "linear" and "team" in params:
    if len(args) < N or args[K] != EXPECTED_DEFAULT:
        raise RuntimeError("linear MCP_SERVERS args layout changed; ...")
    args[K] = str(params["team"])
```

## Surface area

```python
from bandsox.mcp_registry import (
    MCP_SERVERS,              # dict[name, MCPServerEntry]
    MCP_CONFIG_PATH,          # "/workspace/.mcp.json"
    SECRET_ENV_NAMES,         # set[str], auto-seeded from env_map values
    resolve_mcp_config,       # user_mcp -> (mcp_servers, env_vars)
    write_mcp_config_to_rootfs,  # (rootfs_path, {"mcpServers": ...}) -> None
    get_server_config,        # name -> MCPServerEntry | None
)
```

`resolve_mcp_config(None)` and `resolve_mcp_config({})` both return `({}, {})`. The redaction helpers live in [`bandsox/core.py`](../bandsox/core.py) (`_redact_secrets`, `_redact_mcp_servers`) because they're tied to persistence, not to MCP per se.

## See also

- [docs/CLAUDE_CODE.md](CLAUDE_CODE.md) -- end-to-end Claude Code cookbook.
- [docs/API.md](API.md) -- top-level BandSox API reference.
