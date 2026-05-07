import { describe, it, expect, beforeEach, vi } from "vitest";
import { BandSox, MicroVM, BandSoxError } from "../src";

// ─── Helpers ───

function makeResponse(body: unknown, status = 200, contentType = "application/json"): Response {
  const headers = new Headers();
  if (contentType) headers.set("content-type", contentType);
  return new Response(
    typeof body === "string" ? body : JSON.stringify(body),
    { status, headers }
  );
}

function setupFetch() {
  const mock = vi.fn();
  vi.stubGlobal("fetch", mock);
  return mock;
}

// ─── BandSox client ───

describe("BandSox", () => {
  let bs: BandSox;
  let fetchMock: ReturnType<typeof setupFetch>;

  beforeEach(() => {
    fetchMock = setupFetch();
    bs = new BandSox({
      baseUrl: "http://localhost:8000",
      headers: { Authorization: "Bearer bsx_test_key" },
      timeout: 30_000,
    });
  });

  // ── createVm ──

  describe("createVm", () => {
    it("sends correct POST payload and returns MicroVM", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ id: "vm-abc" }));

      const vm = await bs.createVm({
        image: "alpine:latest",
        name: "test-vm",
        vcpu: 2,
        mem_mib: 256,
        enable_networking: false,
        disk_size_mib: 1024,
        metadata: { owner: "alice" },
      });

      expect(vm).toBeInstanceOf(MicroVM);
      expect(vm.vmId).toBe("vm-abc");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms");
      expect(init.method).toBe("POST");
      const body = JSON.parse(init.body as string);
      expect(body.image).toBe("alpine:latest");
      expect(body.name).toBe("test-vm");
      expect(body.vcpu).toBe(2);
      expect(body.mem_mib).toBe(256);
      expect(body.disk_size_mib).toBe(1024);
      expect(body.metadata).toEqual({ owner: "alice" });
    });

    it("omits null fields from request body", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ id: "vm-abc" }));
      await bs.createVm({ image: "alpine:latest" });

      const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
      expect(body).not.toHaveProperty("name");
    });
  });

  // ── createVmFromDockerfile ──

  describe("createVmFromDockerfile", () => {
    it("sends dockerfile as FormData and returns MicroVM", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ id: "vm-df" }));

      const vm = await bs.createVmFromDockerfile(
        "FROM alpine:latest\nRUN echo hi",
        { name: "dockerfile-vm", vcpu: 2 }
      );

      expect(vm).toBeInstanceOf(MicroVM);
      expect(vm.vmId).toBe("vm-df");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/from-dockerfile");
      expect(init.method).toBe("POST");
      expect(init.body).toBeInstanceOf(FormData);
    });
  });

  // ── restoreVm ──

  describe("restoreVm", () => {
    it("restores snapshot and returns MicroVM", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ id: "vm-restored" }));

      const vm = await bs.restoreVm("snap-1", { name: "restored", enable_networking: true });

      expect(vm).toBeInstanceOf(MicroVM);
      expect(vm.vmId).toBe("vm-restored");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/snapshots/snap-1/restore");
      expect(init.method).toBe("POST");
    });
  });

  // ── listVms ──

  describe("listVms", () => {
    it("returns VM info array", async () => {
      const vms = [{ id: "vm-a", name: "a", status: "running" }];
      fetchMock.mockResolvedValueOnce(makeResponse(vms));

      const result = await bs.listVms();
      expect(result).toEqual(vms);
      expect(fetchMock.mock.calls[0][0]).toBe("http://localhost:8000/api/vms");
    });

    it("passes limit and metadata_equals params", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse([]));

      await bs.listVms({ limit: 10, metadata_equals: { env: "prod" } });

      const url = (fetchMock.mock.calls[0] as string[])[0];
      expect(url).toContain("limit=10");
      expect(url).toContain("metadata_equals=%7B%22env%22%3A%22prod%22%7D");
    });
  });

  // ── getVmInfo ──

  describe("getVmInfo", () => {
    it("returns VM info on success", async () => {
      const info = { id: "vm-1", name: "test", status: "running" };
      fetchMock.mockResolvedValueOnce(makeResponse(info));

      const result = await bs.getVmInfo("vm-1");
      expect(result).toEqual(info);
    });

    it("returns null on 404", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({ detail: "VM not found" }, 404)
      );
      const result = await bs.getVmInfo("nonexistent");
      expect(result).toBeNull();
    });
  });

  // ── getVm ──

  describe("getVm", () => {
    it("returns MicroVM with cached info", async () => {
      const info = { id: "vm-1", name: "myvm", status: "running" as const };
      fetchMock.mockResolvedValueOnce(makeResponse(info));

      const vm = await bs.getVm("vm-1");
      expect(vm).toBeInstanceOf(MicroVM);
      expect(vm!.vmId).toBe("vm-1");
      expect(vm!.info).toEqual(info);
    });

    it("returns null when VM not found", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({ detail: "VM not found" }, 404)
      );
      const vm = await bs.getVm("nonexistent");
      expect(vm).toBeNull();
    });
  });

  // ── Stop / Pause / Resume / Delete VM ──

  describe("lifecycle methods", () => {
    it("stopVm sends POST to stop endpoint", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "stopped" }));
      await bs.stopVm("vm-1");
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-1/stop");
      expect(init.method).toBe("POST");
    });

    it("pauseVm sends POST to pause endpoint", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "paused" }));
      await bs.pauseVm("vm-1");
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/vms/vm-1/pause"
      );
    });

    it("resumeVm sends POST to resume endpoint", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "resumed" }));
      await bs.resumeVm("vm-1");
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/vms/vm-1/resume"
      );
    });

    it("deleteVm sends DELETE", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "deleted" }));
      await bs.deleteVm("vm-1");
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-1");
      expect(init.method).toBe("DELETE");
    });
  });

  // ── renameVm / updateVmMetadata ──

  describe("VM metadata", () => {
    it("renameVm sends PUT with name", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "renamed" }));
      await bs.renameVm("vm-1", "new-name");
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-1/name");
      expect(init.method).toBe("PUT");
      const body = JSON.parse(init.body as string);
      expect(body.name).toBe("new-name");
    });

    it("updateVmMetadata sends PUT with metadata", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ owner: "bob" }));
      await bs.updateVmMetadata("vm-1", { owner: "bob", env: "staging" });
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-1/metadata");
      const body = JSON.parse(init.body as string);
      expect(body.metadata).toEqual({ owner: "bob", env: "staging" });
    });
  });

  // ── Snapshots ──

  describe("snapshots", () => {
    it("snapshotVm returns snapshot ID", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ snapshot_id: "snap-xyz" }));
      const snapId = await bs.snapshotVm("vm-1", { name: "my-snap" });
      expect(snapId).toBe("snap-xyz");
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-1/snapshot");
      const body = JSON.parse(init.body as string);
      expect(body.name).toBe("my-snap");
    });

    it("listSnapshots returns snapshot array", async () => {
      const snaps = [{ id: "snap-1", snapshot_name: "s1", metadata: {} }];
      fetchMock.mockResolvedValueOnce(makeResponse(snaps));
      const result = await bs.listSnapshots();
      expect(result).toEqual(snaps);
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/snapshots"
      );
    });

    it("deleteSnapshot sends DELETE", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "deleted" }));
      await bs.deleteSnapshot("snap-1");
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/snapshots/snap-1"
      );
    });

    it("updateSnapshotMetadata sends PUT", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ new: "meta" }));
      await bs.updateSnapshotMetadata("snap-1", { new: "meta" });
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/snapshots/snap-1/metadata");
      expect(init.method).toBe("PUT");
    });

    it("renameSnapshot sends PUT with name", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "renamed" }));
      await bs.renameSnapshot("snap-1", "new-snap-name");
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/snapshots/snap-1/name");
      const body = JSON.parse(init.body as string);
      expect(body.name).toBe("new-snap-name");
    });
  });

  // ── Auth ──

  describe("auth", () => {
    it("authCheck returns authenticated status", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ authenticated: true }));
      const result = await bs.authCheck();
      expect(result).toEqual({ authenticated: true });
    });

    it("login sends password", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "ok", token: "tkn" }));
      const result = await bs.login("secret");
      expect(result.token).toBe("tkn");
      const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
      expect(body.password).toBe("secret");
    });

    it("logout sends POST", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "ok" }));
      await bs.logout();
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/auth/logout"
      );
    });

    it("listApiKeys returns keys", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse([{ key_id: "k1", name: "my-key" }]));
      const result = await bs.listApiKeys();
      expect(result).toEqual([{ key_id: "k1", name: "my-key" }]);
    });

    it("createApiKey returns new key info", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({ key_id: "k2", key: "bsx_secret", name: "new-key" })
      );
      const result = await bs.createApiKey("new-key");
      expect(result.key).toBe("bsx_secret");
      const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
      expect(body.name).toBe("new-key");
    });

    it("revokeApiKey sends DELETE", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "revoked" }));
      await bs.revokeApiKey("k1");
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/auth/keys/k1"
      );
    });
  });

  // ── Error handling ──

  describe("error handling", () => {
    it("throws BandSoxError on non-ok response", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({ detail: "Something went wrong" }, 500)
      );

      await expect(bs.stopVm("vm-1")).rejects.toThrow(BandSoxError);

      fetchMock.mockResolvedValueOnce(
        makeResponse({ detail: "Something went wrong" }, 500)
      );
      await expect(bs.stopVm("vm-1")).rejects.toMatchObject({
        statusCode: 500,
        message: "Something went wrong",
      });
    });

    it("includes statusText as message when no detail in body", async () => {
      fetchMock.mockResolvedValueOnce(
        new Response("plain text", { status: 502, statusText: "Bad Gateway" })
      );

      await expect(bs.listVms()).rejects.toMatchObject({
        statusCode: 502,
        message: "Bad Gateway",
      });
    });
  });

  // ── listProjects ──

  describe("listProjects", () => {
    it("delegates to listVms endpoint", async () => {
      const vms = [{ id: "vm-1", name: "proj" }];
      fetchMock.mockResolvedValueOnce(makeResponse(vms));

      const result = await bs.listProjects();
      expect(result).toEqual(vms);
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/vms"
      );
    });
  });

  // ── baseUrl normalization ──

  describe("baseUrl normalization", () => {
    it("strips trailing slash from baseUrl", async () => {
      const bs2 = new BandSox({
        baseUrl: "http://localhost:8000/",
        headers: {},
      });
      fetchMock.mockResolvedValueOnce(makeResponse([]));

      await bs2.listVms();
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/vms"
      );
    });
  });
});

// ─── MicroVM ───

describe("MicroVM", () => {
  let bs: BandSox;
  let vm: MicroVM;
  let fetchMock: ReturnType<typeof setupFetch>;

  beforeEach(() => {
    fetchMock = setupFetch();
    bs = new BandSox({
      baseUrl: "http://localhost:8000",
      headers: { Authorization: "Bearer bsx_test_key" },
    });
    vm = new MicroVM(
      "vm-test",
      bs,
      {
        id: "vm-test",
        name: "test-vm",
        image: "alpine:latest",
        vcpu: 1,
        mem_mib: 128,
        status: "running",
        network_config: {
          guest_mac: "aa:bb:cc:dd:ee:ff",
          guest_ip: "172.16.1.2",
          host_ip: "172.16.1.1",
          tap_name: "tap0",
        },
        vsock_config: null,
        created_at: 1000,
        pid: 12345,
        agent_ready: true,
        env_vars: null,
        metadata: { owner: "alice" },
      }
    );
  });

  // ── Lifecycle ──

  describe("lifecycle", () => {
    it("stop calls the stop endpoint", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "stopped" }));
      await vm.stop();
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/stop");
      expect(init.method).toBe("POST");
    });

    it("pause calls the pause endpoint", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "paused" }));
      await vm.pause();
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/vms/vm-test/pause"
      );
    });

    it("resume calls the resume endpoint", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "resumed" }));
      await vm.resume();
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/vms/vm-test/resume"
      );
    });

    it("delete calls deleteVm", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "deleted" }));
      await vm.delete();
      expect((fetchMock.mock.calls[0] as string[])[0]).toBe(
        "http://localhost:8000/api/vms/vm-test"
      );
    });

    it("snapshot returns snapshot ID", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ snapshot_id: "snap-1" }));
      const snapId = await vm.snapshot({ name: "checkpoint" });
      expect(snapId).toBe("snap-1");
    });
  });

  // ── Info ──

  describe("getInfo", () => {
    it("fetches and caches VM info", async () => {
      const info = {
        id: "vm-test",
        name: "latest-name",
        image: "alpine",
        vcpu: 2,
        mem_mib: 256,
        status: "running" as const,
        network_config: null,
        vsock_config: null,
        created_at: 2000,
        pid: 99999,
        agent_ready: true,
        env_vars: null,
        metadata: { updated: true },
      };
      fetchMock.mockResolvedValueOnce(makeResponse({ ...info, network_config: null }));

      const result = await vm.getInfo();
      expect(result.name).toBe("latest-name");
      expect(vm.info?.name).toBe("latest-name");
      expect(vm.info?.vcpu).toBe(2);
    });

    it("throws when VM not found", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({ detail: "VM not found" }, 404)
      );
      await expect(vm.getInfo()).rejects.toThrow("VM vm-test not found");
    });
  });

  // ── Metadata ──

  describe("metadata", () => {
    it("rename updates name in cached info", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "renamed" }));
      await vm.rename("bob-vm");
      expect(vm.info!.name).toBe("bob-vm");
    });

    it("updateMetadata updates cached metadata", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ env: "prod" }));
      await vm.updateMetadata({ env: "prod" });
      expect(vm.info!.metadata).toEqual({ env: "prod" });
    });
  });

  // ── execCommand ──

  describe("execCommand", () => {
    it("sends command and returns result", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({ exit_code: 0, stdout: "hello\n", stderr: "" })
      );

      const result = await vm.execCommand("echo hello", 10);
      expect(result.exit_code).toBe(0);
      expect(result.stdout).toBe("hello\n");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/exec");
      const body = JSON.parse(init.body as string);
      expect(body.command).toBe("echo hello");
      expect(body.timeout).toBe(10);
    });

    it("omits timeout when not provided", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({ exit_code: 0, stdout: "", stderr: "" })
      );
      await vm.execCommand("ls");
      const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
      expect(body.timeout).toBeUndefined();
    });
  });

  // ── execPython ──

  describe("execPython", () => {
    it("sends Python code with options", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({
          exit_code: 0,
          stdout: "pyout",
          stderr: "",
          output: "pyout",
          success: true,
          error: null,
        })
      );

      const result = await vm.execPython({
        code: "print('hi')",
        packages: ["requests"],
        timeout: 30,
        cwd: "/app",
      });

      expect(result.success).toBe(true);
      expect(result.stdout).toBe("pyout");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/exec-python");
      const body = JSON.parse(init.body as string);
      expect(body.code).toBe("print('hi')");
      expect(body.packages).toEqual(["requests"]);
      expect(body.cwd).toBe("/app");
    });
  });

  // ── File operations ──

  describe("listDir", () => {
    it("returns directory listing", async () => {
      const listing = {
        path: "/app",
        files: [
          { name: "main.py", path: "/app/main.py", size: 100, is_dir: false, is_file: true, mtime: 1 },
        ],
      };
      fetchMock.mockResolvedValueOnce(makeResponse(listing));

      const result = await vm.listDir("/app");
      expect(result).toEqual(listing);
      expect((fetchMock.mock.calls[0] as string[])[0]).toContain("path=%2Fapp");
    });
  });

  describe("readFile", () => {
    it("returns file content as string", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ path: "/etc/hosts", content: "hosts content" }));

      const content = await vm.readFile("/etc/hosts");
      expect(content).toBe("hosts content");
      expect((fetchMock.mock.calls[0] as string[])[0]).toContain("path=%2Fetc%2Fhosts");
    });
  });

  describe("writeFile", () => {
    it("writes content with options", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "written" }));

      await vm.writeFile({
        path: "/app/config.json",
        content: '{"debug": true}',
        encoding: "utf-8",
        append: false,
      });

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/write-file");
      const body = JSON.parse(init.body as string);
      expect(body.path).toBe("/app/config.json");
      expect(body.content).toBe('{"debug": true}');
    });

    it("writeFile with append flag", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "appended" }));

      await vm.writeFile({ path: "/log.txt", content: "line\n", append: true });

      const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
      expect(body.append).toBe(true);
    });

    it("writeFile with base64 encoding", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "written" }));

      await vm.writeFile({ path: "/data.bin", content: "aGVsbG8=", encoding: "base64" });

      const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
      expect(body.encoding).toBe("base64");
    });
  });

  describe("appendFile", () => {
    it("sends append request", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "appended" }));

      await vm.appendFile("/log.txt", "more\n");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/append-file");
      const body = JSON.parse(init.body as string);
      expect(body.path).toBe("/log.txt");
      expect(body.content).toBe("more\n");
      expect(body.append).toBe(true);
    });

    it("appendFile with base64", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "appended" }));

      await vm.appendFile("/data.bin", "aGVsbG8=", "base64");

      const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
      expect(body.encoding).toBe("base64");
    });
  });

  describe("getFileInfo", () => {
    it("returns file metadata", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ path: "/f", info: { size: 256, mode: 0o644 } }));

      const info = await vm.getFileInfo("/f");
      expect(info).toEqual({ size: 256, mode: 0o644 });
    });
  });

  describe("downloadFile", () => {
    it("downloads file as Uint8Array", async () => {
      fetchMock.mockResolvedValueOnce(
        new Response(new Uint8Array([1, 2, 3, 4]), {
          headers: { "content-type": "application/octet-stream" },
        })
      );

      const data = await vm.downloadFile("/app/data.bin");
      expect(data).toBeInstanceOf(Uint8Array);
      expect(data).toEqual(new Uint8Array([1, 2, 3, 4]));
    });
  });

  describe("uploadFile", () => {
    it("uploads string content as FormData", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "uploaded" }));

      await vm.uploadFile("/app/script.py", "print('hello')");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/upload");
      expect(init.method).toBe("POST");
      expect(init.body).toBeInstanceOf(FormData);
    });

    it("uploads Uint8Array content as FormData", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "uploaded" }));

      await vm.uploadFile("/app/data.bin", new Uint8Array([1, 2, 3]));

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/upload");
      expect(init.body).toBeInstanceOf(FormData);
    });
  });

  describe("uploadFolder", () => {
    it("uploads multiple files with directory creation", async () => {
      fetchMock
        .mockResolvedValueOnce(makeResponse({ exit_code: 0, stdout: "", stderr: "" })) // mkdir -p
        .mockResolvedValueOnce(makeResponse({ exit_code: 0, stdout: "", stderr: "" })) // mkdir -p
        .mockResolvedValueOnce(makeResponse({ status: "uploaded" })) // upload main.py
        .mockResolvedValueOnce(makeResponse({ exit_code: 0, stdout: "", stderr: "" })) // mkdir -p
        .mockResolvedValueOnce(makeResponse({ status: "uploaded" })); // upload utils.py

      await vm.uploadFolder(
        {
          "main.py": 'print("hello")',
          "lib/utils.py": "def add(a,b): return a+b",
        },
        "/app"
      );

      // Check directory creation calls
      const calls = fetchMock.mock.calls;
      const urls = calls.map((c: unknown[]) => (c as string[])[0]);

      // First call: mkdir -p /app
      expect(urls[0]).toBe("http://localhost:8000/api/vms/vm-test/exec");
      const body0 = JSON.parse((calls[0] as RequestInit[])[1].body as string);
      expect(body0.command).toBe("mkdir -p /app");

      // Third call: upload main.py
      expect(urls[2]).toBe("http://localhost:8000/api/vms/vm-test/upload");
    });
  });

  // ── Networking ──

  describe("waitForAgent", () => {
    it("returns true when agent is ready", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({
          id: "vm-test", name: "test", image: "alpine", vcpu: 1, mem_mib: 128,
          status: "running", network_config: null, vsock_config: null,
          created_at: 1000, pid: 123, agent_ready: true, env_vars: null, metadata: {},
        })
      );

      const result = await vm.waitForAgent(2);
      expect(result).toBe(true);
    });

    it("returns false after timeout", async () => {
      // Every getInfo() call returns agent_ready: false, need fresh Response each time
      const info = {
        id: "vm-test", name: "test", image: "alpine", vcpu: 1, mem_mib: 128,
        status: "starting", network_config: null, vsock_config: null,
        created_at: 1000, pid: 123, agent_ready: false, env_vars: null, metadata: {},
      };
      fetchMock.mockImplementation(() => Promise.resolve(makeResponse(info)));

      const result = await vm.waitForAgent(1);
      expect(result).toBe(false);
    });
  });

  describe("appendText", () => {
    it("delegates to writeFile with append=true", async () => {
      fetchMock.mockResolvedValueOnce(makeResponse({ status: "appended" }));

      await vm.appendText("/log.txt", "appended line\n");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/write-file");
      const body = JSON.parse(init.body as string);
      expect(body.path).toBe("/log.txt");
      expect(body.content).toBe("appended line\n");
      expect(body.append).toBe(true);
    });
  });

  // ── Networking ──

  describe("getGuestIp", () => {
    it("returns guest IP from cached info", () => {
      expect(vm.getGuestIp()).toBe("172.16.1.2");
    });

    it("returns null when no network config", () => {
      const vm2 = new MicroVM("vm-nonet", bs);
      expect(vm2.getGuestIp()).toBeNull();
    });
  });

  describe("sendHttpRequest", () => {
    it("proxies HTTP request to guest", async () => {
      fetchMock.mockResolvedValueOnce(
        makeResponse({
          status_code: 200,
          headers: { "x-custom": "val" },
          body: "OK",
        })
      );

      const result = await vm.sendHttpRequest({
        port: 8080,
        path: "/api/health",
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: '{"key":"val"}',
      });

      expect(result.status_code).toBe(200);
      expect(result.body).toBe("OK");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("http://localhost:8000/api/vms/vm-test/http");
      const body = JSON.parse(init.body as string);
      expect(body.port).toBe(8080);
      expect(body.path).toBe("/api/health");
      expect(body.method).toBe("POST");
    });
  });

  // ── BandSoxError ──

  describe("BandSoxError", () => {
    it("has statusCode and message", () => {
      const err = new BandSoxError("Not Found", 404);
      expect(err).toBeInstanceOf(Error);
      expect(err.name).toBe("BandSoxError");
      expect(err.statusCode).toBe(404);
      expect(err.message).toBe("Not Found");
    });
  });
});

// ─── Edge cases ───

describe("edge cases", () => {
  let bs: BandSox;
  let fetchMock: ReturnType<typeof setupFetch>;

  beforeEach(() => {
    fetchMock = setupFetch();
    bs = new BandSox({ baseUrl: "http://localhost:8000", headers: {} });
  });

  it("handles empty headers config", async () => {
    fetchMock.mockResolvedValueOnce(makeResponse([]));
    await bs.listVms();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // Should not have Authorization header
    expect((init.headers as Record<string, string>)?.Authorization).toBeUndefined();
    expect(url).toBe("http://localhost:8000/api/vms");
  });

  it("handles empty response array for listVms", async () => {
    fetchMock.mockResolvedValueOnce(makeResponse([]));
    const result = await bs.listVms();
    expect(result).toEqual([]);
  });

  it("handles snapshot with metadata", async () => {
    fetchMock.mockResolvedValueOnce(makeResponse({ snapshot_id: "snap-meta" }));
    const snapId = await bs.snapshotVm("vm-1", {
      name: "label",
      metadata: { reason: "backup" },
    });
    expect(snapId).toBe("snap-meta");

    const body = JSON.parse((fetchMock.mock.calls[0] as RequestInit[])[1].body as string);
    expect(body.metadata).toEqual({ reason: "backup" });
  });

  it("headers are passed through to requests", async () => {
    const authBs = new BandSox({
      baseUrl: "http://localhost:8000",
      headers: { "X-Custom": "value", Authorization: "Bearer key" },
    });
    fetchMock.mockResolvedValueOnce(makeResponse([]));

    await authBs.listVms();
    const init = (fetchMock.mock.calls[0] as RequestInit[])[1];
    const h = init.headers as Record<string, string>;
    expect(h["X-Custom"]).toBe("value");
    expect(h["Authorization"]).toBe("Bearer key");
  });
});
