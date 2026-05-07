import {
  ExecResult,
  ExecPythonOptions,
  ExecPythonResult,
  FileInfo,
  VmInfo,
  ListDirResult,
  WriteFileOptions,
  HttpProxyOptions,
  HttpProxyResult,
  UploadFolderFiles,
  SnapshotOptions,
} from "./types";
import { BandSox } from "./client";

export class MicroVM {
  readonly vmId: string;
  private bandsox: BandSox;
  info: VmInfo | null;

  constructor(vmId: string, bandsox: BandSox, info?: VmInfo) {
    this.vmId = vmId;
    this.bandsox = bandsox;
    this.info = info ?? null;
  }

  // ─── Lifecycle ───

  async stop(): Promise<void> {
    await this.bandsox._request("POST", `/api/vms/${this.vmId}/stop`);
  }

  async pause(): Promise<void> {
    await this.bandsox._request("POST", `/api/vms/${this.vmId}/pause`);
  }

  async resume(): Promise<void> {
    await this.bandsox._request("POST", `/api/vms/${this.vmId}/resume`);
  }

  async delete(): Promise<void> {
    await this.bandsox.deleteVm(this.vmId);
  }

  async snapshot(options: SnapshotOptions): Promise<string> {
    return this.bandsox.snapshotVm(this.vmId, options);
  }

  // ─── Info ───

  async getInfo(): Promise<VmInfo> {
    const info = await this.bandsox.getVmInfo(this.vmId);
    if (!info) {
      throw new Error(`VM ${this.vmId} not found`);
    }
    this.info = info;
    return info;
  }

  async waitForAgent(timeout: number = 30): Promise<boolean> {
    const start = Date.now();
    while (Date.now() - start < timeout * 1000) {
      const info = await this.getInfo();
      if (info.agent_ready || info.status === "running") {
        return true;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return false;
  }

  // ─── Metadata ───

  async rename(newName: string): Promise<void> {
    await this.bandsox.renameVm(this.vmId, newName);
    if (this.info) {
      this.info.name = newName;
    }
  }

  async updateMetadata(
    metadata: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    const result = await this.bandsox.updateVmMetadata(this.vmId, metadata);
    if (this.info) {
      this.info.metadata = metadata;
    }
    return result;
  }

  // ─── Commands ───

  async execCommand(
    command: string,
    timeout?: number
  ): Promise<ExecResult> {
    return this.bandsox._request<ExecResult>(
      "POST",
      `/api/vms/${this.vmId}/exec`,
      {
        json: { command, timeout: timeout ?? null },
        timeout: (timeout ?? 30) * 1000 + 5000,
      }
    );
  }

  async execPython(options: ExecPythonOptions): Promise<ExecPythonResult> {
    const payload = {
      code: options.code,
      cwd: options.cwd ?? null,
      packages: options.packages ?? null,
      timeout: options.timeout ?? null,
      cleanup_venv: options.cleanup_venv ?? null,
    };
    return this.bandsox._request<ExecPythonResult>(
      "POST",
      `/api/vms/${this.vmId}/exec-python`,
      {
        json: payload,
        timeout: (options.timeout ?? 60) * 1000 + 5000,
      }
    );
  }

  // ─── File operations ───

  async listDir(path?: string): Promise<ListDirResult> {
    const params: Record<string, string | null> = {};
    if (path) {
      params["path"] = path;
    }
    return this.bandsox._request<ListDirResult>(
      "GET",
      `/api/vms/${this.vmId}/files`,
      { params }
    );
  }

  async readFile(path: string): Promise<string> {
    const res = await this.bandsox._request<{ content: string }>(
      "GET",
      `/api/vms/${this.vmId}/read-file`,
      { params: { path } }
    );
    return res.content;
  }

  async writeFile(options: WriteFileOptions): Promise<void> {
    await this.bandsox._request(
      "POST",
      `/api/vms/${this.vmId}/write-file`,
      { json: options }
    );
  }

  async appendFile(
    path: string,
    content: string,
    encoding?: "utf-8" | "base64"
  ): Promise<void> {
    await this.bandsox._request(
      "POST",
      `/api/vms/${this.vmId}/append-file`,
      { json: { path, content, encoding: encoding ?? "utf-8", append: true } }
    );
  }

  async appendText(path: string, content: string): Promise<void> {
    await this.writeFile({ path, content, append: true });
  }

  async getFileInfo(path: string): Promise<FileInfo> {
    const res = await this.bandsox._request<{ info: FileInfo }>(
      "GET",
      `/api/vms/${this.vmId}/file-info`,
      { params: { path } }
    );
    return res.info;
  }

  async downloadFile(remotePath: string): Promise<Uint8Array> {
    const resp = await this.bandsox._rawRequest(
      "GET",
      `/api/vms/${this.vmId}/download`,
      { params: { path: remotePath }, timeout: 300_000 }
    );
    const buffer = await resp.arrayBuffer();
    return new Uint8Array(buffer);
  }

  async uploadFile(
    remotePath: string,
    content: Uint8Array | string
  ): Promise<void> {
    const data =
      typeof content === "string"
        ? new TextEncoder().encode(content)
        : content;
    const formData = new FormData();
    formData.append("remote_path", remotePath);
    formData.append("file", new Blob([data as BlobPart]), remotePath.split("/").pop() ?? "file");
    await this.bandsox._request(
      "POST",
      `/api/vms/${this.vmId}/upload`,
      { formData }
    );
  }

  async uploadFolder(
    files: UploadFolderFiles,
    remoteBase: string
  ): Promise<void> {
    // First create the base directory
    await this.execCommand(`mkdir -p ${remoteBase}`);

    for (const [relativePath, content] of Object.entries(files)) {
      const remotePath = `${remoteBase.replace(/\/$/, "")}/${relativePath.replace(/^\//, "")}`;
      const dir = remotePath.substring(0, remotePath.lastIndexOf("/"));
      if (dir) {
        await this.execCommand(`mkdir -p ${dir}`);
      }
      await this.uploadFile(remotePath, content);
    }
  }

  // ─── Networking ───

  getGuestIp(): string | null {
    return this.info?.network_config?.guest_ip ?? null;
  }

  async sendHttpRequest(
    options: HttpProxyOptions
  ): Promise<HttpProxyResult> {
    return this.bandsox._request<HttpProxyResult>(
      "POST",
      `/api/vms/${this.vmId}/http`,
      {
        json: {
          port: options.port,
          path: options.path ?? "/",
          method: options.method ?? "GET",
          headers: options.headers ?? null,
          body: options.body ?? null,
          json_body: options.json_body ?? null,
          timeout: options.timeout ?? null,
        },
        timeout: (options.timeout ?? 30) * 1000 + 5000,
      }
    );
  }
}
