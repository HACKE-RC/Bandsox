# BandSox TypeScript SDK

TypeScript client for managing Firecracker microVMs through the BandSox REST API. This is the JS/TS counterpart to the Python `BandSox` library -- it talks to the same server and covers the same operations.

## Requirements

- Node.js 18+ (uses the built-in `fetch` API)
- A running BandSox server (`sudo python3 -m bandsox.cli serve`)
- An API key (generated on first server start, or via `bandsox auth create-key`)

## Installation

```bash
npm install bandsox
```

## Quick start

```ts
import { BandSox } from "bandsox";

const bs = new BandSox({
  baseUrl: "http://localhost:8000",
  headers: { Authorization: "Bearer bsx_your_api_key_here" },
});

// Spin up a VM from a Docker image
const vm = await bs.createVm({ image: "python:3-alpine", name: "my-sandbox" });

// Run a command
const result = await vm.execCommand("echo Hello from Firecracker!");
console.log(result.stdout); // "Hello from Firecracker!\n"

// Clean up
await vm.stop();
await vm.delete();
```

## Usage guide

### Connecting to the server

The `BandSox` class is your entry point. Point it at your BandSox server and pass your API key.

```ts
import { BandSox } from "bandsox";

const bs = new BandSox({
  baseUrl: "http://localhost:8000",
  headers: { Authorization: "Bearer bsx_your_api_key_here" },
  timeout: 120_000, // request timeout in ms, defaults to 60s
});
```

The API key is required. Generate one with `bandsox auth create-key <name>` on the server, or use the initial key printed on first server start.

### Creating VMs

**From a Docker image:**

```ts
const vm = await bs.createVm({
  image: "python:3.11-slim",
  name: "my-python-sandbox",
  vcpu: 2,
  mem_mib: 512,
  disk_size_mib: 4096,
  enable_networking: true,
  metadata: { owner: "alice", project: "ml-pipeline" },
});
```

**From a Dockerfile:**

```ts
import { readFileSync } from "fs";

const dockerfile = readFileSync("./Dockerfile", "utf-8");
const vm = await bs.createVmFromDockerfile(dockerfile, {
  tag: "custom-image:v1",
  name: "my-custom-vm",
});
```

### Running commands

**Shell commands** return stdout, stderr, and the exit code:

```ts
const res = await vm.execCommand("ls -la /", 10);
console.log(res.stdout);
console.log(res.exit_code); // 0
```

**Python code** runs in an isolated venv with optional package installation:

```ts
const res = await vm.execPython({
  code: `
import requests
r = requests.get("https://api.github.com")
print(r.status_code)
  `,
  packages: ["requests"],
  timeout: 120,
});

if (res.success) {
  console.log(res.stdout); // "200\n"
} else {
  console.error(res.error);
}
```

### File operations

```ts
// Write a file
await vm.writeFile({ path: "/app/config.json", content: '{"debug": true}' });

// Append text using a convenience helper
await vm.appendText("/app/config.json", "\n{\"feature\": \"fast-read\"}");

// Append with explicit encoding (utf-8 or base64)
await vm.appendFile("/app/config.json", "\n{\"mode\": \"append\"}", "utf-8");

// Read it back
const content = await vm.readFile("/app/config.json");

// List a directory
const listing = await vm.listDir("/app");
for (const entry of listing.files) {
  console.log(`${entry.name}  ${entry.size} bytes  dir=${entry.is_dir}`);
}

// Get file metadata
const info = await vm.getFileInfo("/app/config.json");

// Binary download
const bytes: Uint8Array = await vm.downloadFile("/app/data.bin");

// Binary upload
await vm.uploadFile("/app/data.bin", bytes);

// Upload a whole folder
await vm.uploadFolder(
  {
    "src/main.py": "print('hello')",
    "src/lib/utils.py": "def add(a, b): return a + b",
    "README.md": "# My Project",
  },
  "/app"
);
```

### Networking

If the VM was created with `enable_networking: true`, you can reach services running inside it.

```ts
// Check the guest IP (requires a prior getInfo() call)
await vm.getInfo();
console.log(vm.getGuestIp()); // "172.16.x.x"

// Proxy an HTTP request to a service running on port 8080 inside the VM
const resp = await vm.sendHttpRequest({
  port: 8080,
  path: "/api/health",
  method: "GET",
});
console.log(resp.status_code, resp.body);
```

### Snapshots

Save the full memory and disk state of a running VM and restore it later.

```ts
// Snapshot
const snapId = await vm.snapshot({ name: "checkpoint-1" });

// Stop the original
await vm.stop();

// Restore into a new VM
const restored = await bs.restoreVm(snapId, { name: "restored-vm" });
const res = await restored.execCommand("cat /tmp/my-state-file");
```

### Pause and resume

```ts
await vm.pause();
// VM is frozen -- no CPU cycles, no network timeouts
await vm.resume();
// Back to normal
```

### VM metadata

Attach arbitrary key-value data to VMs for filtering and bookkeeping.

```ts
await vm.updateMetadata({ env: "staging", owner: "bob" });
await vm.rename("staging-worker-1");

// Find VMs by metadata
const vms = await bs.listVms({ metadata_equals: { env: "staging" } });
```

### Lifecycle shortcuts

The `BandSox` client has convenience methods that operate on a VM by ID, without needing a `MicroVM` handle:

```ts
await bs.stopVm(vmId);
await bs.pauseVm(vmId);
await bs.resumeVm(vmId);
await bs.deleteVm(vmId);
await bs.renameVm(vmId, "new-name");
await bs.updateVmMetadata(vmId, { deployed: true });
```

You can also wait for a freshly created VM to become interactive:

```ts
const ready = await vm.waitForAgent(30);
if (!ready) throw new Error("VM did not become ready in time");
```

### Authentication helpers

```ts
// Check whether current credentials/session are authenticated
const status = await bs.authCheck();

// Programmatic login/logout for session-based flows
await bs.login("admin-password");
await bs.logout();

// API key management (requires existing auth)
const created = await bs.createApiKey("ci-runner");
const keys = await bs.listApiKeys();
await bs.revokeApiKey(created.key_id);
```

### Error handling

All API errors throw `BandSoxError`, which carries the HTTP status code:

```ts
import { BandSoxError } from "bandsox";

try {
  await bs.getVm("nonexistent-id");
} catch (err) {
  if (err instanceof BandSoxError) {
    console.log(err.statusCode); // 404
    console.log(err.message);    // "VM not found"
  }
}
```

## API reference

### `BandSox`

| Method | Description |
| --- | --- |
| `createVm(options)` | Create a VM from a Docker image. |
| `createVmFromDockerfile(dockerfile, options?)` | Build from a Dockerfile and create a VM. |
| `restoreVm(snapshotId, options?)` | Restore a VM from a snapshot. |
| `getVm(vmId)` | Get a `MicroVM` handle with cached info. |
| `getVmInfo(vmId)` | Get raw VM details as `VmInfo`. |
| `listProjects(options?)` | Alias of `listVms(options?)` (used by UI/project views). |
| `listVms(options?)` | List VMs. Supports `limit` and `metadata_equals` filters. |
| `deleteVm(vmId)` | Delete a VM and its resources. |
| `renameVm(vmId, newName)` | Rename a VM. |
| `updateVmMetadata(vmId, metadata)` | Replace a VM's metadata. |
| `stopVm(vmId)` | Stop a VM. |
| `pauseVm(vmId)` | Pause a VM. |
| `resumeVm(vmId)` | Resume a paused VM. |
| `snapshotVm(vmId, options)` | Snapshot a running VM. Returns the snapshot ID. |
| `listSnapshots()` | List all snapshots. |
| `deleteSnapshot(snapshotId)` | Delete a snapshot. |
| `updateSnapshotMetadata(snapshotId, metadata)` | Update snapshot metadata. |
| `renameSnapshot(snapshotId, newName)` | Rename a snapshot. |
| `authCheck()` | Check auth state for current request context. |
| `login(password)` | Log in and receive session token/cookie response. |
| `logout()` | Log out the current session. |
| `listApiKeys()` | List API keys. |
| `createApiKey(name)` | Create an API key. |
| `revokeApiKey(keyId)` | Revoke an API key. |

### `MicroVM`

| Method | Description |
| --- | --- |
| `stop()` | Stop the VM. |
| `pause()` | Pause the VM. |
| `resume()` | Resume the VM. |
| `delete()` | Delete the VM. |
| `snapshot(options)` | Snapshot. Returns snapshot ID. |
| `getInfo()` | Fetch current VM info from the server. |
| `waitForAgent(timeout?)` | Poll until the VM agent is ready (or timeout). |
| `info` | Cached `VmInfo` from the last `getInfo()` call. |
| `rename(newName)` | Rename the VM. |
| `updateMetadata(metadata)` | Replace VM metadata. |
| `execCommand(command, timeout?)` | Run a shell command. Returns `ExecResult`. |
| `execPython(options)` | Run Python code with optional packages. Returns `ExecPythonResult`. |
| `listDir(path?)` | List directory contents. |
| `readFile(path)` | Read a file as a string. |
| `writeFile(options)` | Write a string to a file. Supports utf-8 and base64 encoding. |
| `appendFile(path, content, encoding?)` | Append content directly with explicit encoding. |
| `appendText(path, content)` | Append UTF-8 text convenience wrapper. |
| `getFileInfo(path)` | Get file metadata (size, mode, mtime). |
| `downloadFile(remotePath)` | Download a file as `Uint8Array`. |
| `uploadFile(remotePath, content)` | Upload a `Uint8Array` or string. |
| `uploadFolder(files, remoteBase)` | Upload multiple files from a `{ path: content }` map. |
| `sendHttpRequest(options)` | Proxy an HTTP request to a port inside the VM. |
| `getGuestIp()` | Guest IP from cached info (call `getInfo()` first). |

### Key types

```ts
interface ExecResult {
  exit_code: number;
  stdout: string;
  stderr: string;
}

interface ExecPythonResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  output: string;
  success: boolean;
  error: string | null;
}

interface VmInfo {
  id: string;
  name: string | null;
  image: string;
  vcpu: number;
  mem_mib: number;
  status: "running" | "paused" | "stopped";
  network_config: NetworkConfig | null;
  metadata: Record<string, unknown>;
  // ... and more
}

interface HttpProxyResult {
  status_code: number;
  headers: Record<string, string>;
  body: string;
}
```

Full type definitions are in [`src/types.ts`](src/types.ts).

## Python equivalent

Every method in this SDK maps to its Python counterpart:

| TypeScript | Python |
| --- | --- |
| `bs.createVm({ image })` | `bs.create_vm("image")` |
| `bs.restoreVm(snapId)` | `bs.restore_vm(snap_id)` |
| `vm.execCommand("ls")` | `vm.exec_command("ls")` |
| `vm.execPython({ code, packages })` | `vm.exec_python_capture(code, packages=...)` |
| `vm.readFile("/etc/hosts")` | `vm.get_file_contents("/etc/hosts")` |
| `vm.appendText(path, text)` | `vm.append_text(path, text)` |
| `vm.appendFile(path, content, "base64")` | `vm.write_bytes(path, data, append=True)` |
| `vm.uploadFile(path, data)` | `vm.upload_file(local, remote)` |
| `vm.sendHttpRequest({ port })` | `vm.send_http_request(port=...)` |
| `vm.snapshot({ name })` | `bs.snapshot_vm(vm, name)` |

## License

Apache License 2.0
