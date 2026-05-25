# Claude Code

> Run Anthropic's Claude Code agentic coding CLI inside secure Bandsox Firecracker microVM sandboxes.

Bandsox gives you kernel-level isolation for Claude Code sessions with millisecond boot times and instant snapshot/restore. This matches the "run Claude Code in a sandbox" pattern from E2B and others, while adding VM-level pause + snapshot for agentic workflows that can span hours or days.

## Prerequisites

- An Anthropic API key (export `ANTHROPIC_API_KEY`)
- Bandsox installed (`pip install bandsox` or from source)
- (Recommended) The published image `ghcr.io/bandsox/claude-code:latest` (or build the template locally)

## Quickstart (headless one-shot)

```python
from bandsox.core import BandSox

bs = BandSox()
vm = bs.create_vm(
    "ghcr.io/bandsox/claude-code:latest",
    env_vars={"ANTHROPIC_API_KEY": "sk-ant-..."},
)

result = vm.exec_command(
    "claude --dangerously-skip-permissions -p 'Create a minimal FastAPI hello world server with one endpoint'",
    timeout=300,
)
print(result["stdout"])
vm.stop()
```

TypeScript (via the SDK):

```ts
import BandSox from 'bandsox';

const bs = new BandSox({ baseUrl: 'http://localhost:8000' });
const vm = await bs.createVm({
  image: 'ghcr.io/bandsox/claude-code:latest',
  env_vars: { ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY! },
});

const res = await vm.execCommand(
  "claude --dangerously-skip-permissions -p 'Create a minimal FastAPI hello world server'",
  { timeout: 300000 }
);
console.log(res.stdout);
await vm.stop();
```

## Using the local template (no published image yet)

```python
vm = bs.create_vm_from_dockerfile(
    "templates/claude-code/Dockerfile",
    env_vars={"ANTHROPIC_API_KEY": "..."},
)
# ... same exec as above
```

The template installs Node 24 + git/curl/ripgrep + `@anthropic-ai/claude-code` globally and sets WORKDIR /workspace.

## Working with a Git repository

```python
vm.exec_command("git clone https://github.com/your/repo.git /workspace/repo")
vm.exec_command("cd /workspace/repo && claude --dangerously-skip-permissions -p 'Review the code and add a README section about performance'")
```

All changes stay inside the VM. Pull them out with `vm.get_file_contents`, `vm.download_file`, or the fastread path.

## MCP tools

Pass the `mcp` argument at creation time. Bandsox resolves known servers and writes a project-scope `/workspace/.mcp.json` into the VM rootfs before boot. Because `WORKDIR` in the template is `/workspace`, Claude Code picks it up automatically.

```python
vm = bs.create_vm(
    "ghcr.io/bandsox/claude-code:latest",
    env_vars={"ANTHROPIC_API_KEY": "..."},
    mcp={
        "browserbase": {"apiKey": "...", "projectId": "..."},
        "github": {"token": "ghp_..."},
    },
)
```

Supported out of the box: `browserbase`, `fetch` (needs `uvx` in the image), `filesystem` (pass `{"path": "/some/dir"}` to override the allowed directory), `github`, `slack` (upstream archived).

For servers not in the registry, pass a raw spec under the `spec` key:

```python
mcp={"my-custom": {"spec": {"command": "uvx", "args": ["my-mcp"], "env": {"FOO": "bar"}}}}
```

Unknown server names raise `ValueError` (typos do not silently no-op). MCP-derived credentials are kept out of persisted VM metadata.

## Snapshot + resume (Bandsox superpower)

Claude Code supports its own `--resume` / session continuation. Bandsox makes the entire environment (filesystem + any Claude Code state files) snapshot-restorable in milliseconds.

```python
# ... long running task inside the VM ...
vm.pause()
snap = vm.snapshot("/snapshots/claude-task-42")

# hours or days later, on another machine or after reboot:
restored = bs.restore_vm(snap)
# The Claude Code working tree, any .claude/ state, and open files are exactly as left.
restored.exec_command("claude --dangerously-skip-permissions --resume 'Continue the previous task'")
```

This is the key differentiator versus container-based sandboxes: true VM snapshots give you durable, forkable, auditable agent sessions with full kernel isolation.

### Snapshot RNG safety

When two VMs are restored from the same snapshot, the kernel CRNG state is identical — without intervention, both copies would produce identical `/dev/urandom` bytes, identical TLS session keys, etc. Bandsox handles this by mixing a fresh per-restore host seed into both kernel pools at restore time. The mix uses only `coreutils` (`base64`, `printf`) so it works in any image, including the claude-code template which has no `python3`. If `python3` is also present, the kernel additionally credits the entropy via the `RNDADDENTROPY` ioctl.

You can verify the divergence by restoring the same snapshot twice and reading `head -c 32 /dev/urandom` from each: the outputs will differ.

## Notes

- The default working directory inside these images is `/workspace`.
- For unattended runs, `--dangerously-skip-permissions` is the documented flag (exactly as shown in E2B and Anthropic examples). Claude Code refuses this flag when running as `root`; set `IS_SANDBOX=1` in the VM env to acknowledge you're inside an isolated sandbox.
- Network egress is enabled by default (`enable_networking=True`); pass `False` for fully air-gapped sessions.
- The template installs `haveged`, and bandsox's `/init` shim auto-starts it so the kernel CRNG is unblocked before Claude Code's first TLS handshake. Without this, fresh microVMs running the 2021-era Firecracker quickstart kernel can hang on HTTPS for tens of seconds. Custom images that don't include `haveged` (or `rng-tools`) will see this stall.
- All the usual Bandsox primitives (fast file read/write over vsock, exec streaming, PTY sessions) are available if you need to drive or observe the Claude Code process from the host.

See the main README and `bandsox --help` for more VM lifecycle commands.
