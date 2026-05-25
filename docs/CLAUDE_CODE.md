# Claude Code

> Run Anthropic's Claude Code CLI inside Bandsox Firecracker microVM sandboxes.

This is roughly the same shape as E2B's `claude-code` template: pull a prebuilt image, set `ANTHROPIC_API_KEY`, run `claude -p '...'`. The thing Bandsox adds is VM-level pause and snapshot, which lets you save a half-finished session and pick it up later on a different host.

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

For the full list of registered servers and instructions on adding new ones, see [MCP_REGISTRY.md](MCP_REGISTRY.md).

## Snapshot and resume

Claude Code has its own `--resume` flag for picking up a previous session. Bandsox snapshots the whole VM around it, so you can stop a long-running session, restore it later, and find the filesystem, any `.claude/` state, and open processes in the same shape they were when you paused.

```python
# ... long running task inside the VM ...
vm.pause()
snap = vm.snapshot("/snapshots/claude-task-42")

# Hours or days later, on a different machine if you want:
restored = bs.restore_vm(snap)
restored.exec_command("claude --dangerously-skip-permissions --resume 'Continue the previous task'")
```

Why bother with VM snapshots instead of containers: container checkpoints don't capture kernel state, so anything that depends on PIDs, namespaces, or open network sockets tends to break across the restore boundary. Firecracker snapshots include the full kernel + memory + disk image, which is what makes the pause/resume actually round-trip.

### Snapshot RNG safety

Two VMs restored from the same snapshot start with identical kernel CRNG state. Without intervention, both copies would produce the same `/dev/urandom` bytes, the same TLS session keys, and so on, until the kernel collects new interrupt entropy.

Bandsox handles that by mixing a fresh per-restore host seed into both `/dev/random` and `/dev/urandom` at restore time. The mix is a shell `printf | base64 -d` pipeline, so it works on any image, including the claude-code template which has no `python3`. When `python3` is present, the kernel additionally credits the entropy through the `RNDADDENTROPY` ioctl.

To check that the divergence actually happened: restore the same snapshot twice and read `head -c 32 /dev/urandom` from each VM. The outputs will differ.

## Things to know

- The working directory inside the image is `/workspace`. The MCP config lands there as `.mcp.json` and Claude Code reads it from the cwd.
- For unattended runs use `--dangerously-skip-permissions` (the flag E2B and the Anthropic docs both show). Claude Code refuses it when it sees uid 0, so either run as a non-root user inside the VM or set `IS_SANDBOX=1` in the env to acknowledge you're isolated.
- Network egress is on by default (`enable_networking=True`). Pass `False` for an air-gapped run.
- The template installs `haveged` and bandsox's `/init` shim starts it before the agent. Without that, the 2021 Firecracker quickstart kernel can leave `crng_init=0` for tens of seconds, and Claude Code's first TLS handshake just hangs. If you build your own image, install `haveged` (or `rng-tools`) or accept the stall.
- Everything else BandSox exposes still works: file read/write over vsock, exec streaming, PTY sessions. Use them if you need to drive Claude Code from the host instead of `-p` one-shots.

The main README and `bandsox --help` cover the rest of the VM lifecycle.
