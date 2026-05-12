import type {
  BandSoxConfig,
  CreateVmOptions,
  CreateVmFromDockerfileOptions,
  RestoreVmOptions,
  ListVmsOptions,
  SnapshotOptions,
  VmInfo,
  SnapshotInfo,
  AuthCheckResult,
  AuthKeysResult,
  CreateApiKeyResult,
} from "./types";
import { BandSoxError } from "./error";
import { MicroVM } from "./microvm";
import { TerminalSession } from "./terminal";

const SESSION_COOKIE_NAME = "bandsox_session";

function stripNulls(obj: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj)) {
    if (value !== null) {
      result[key] = value;
    }
  }
  return result;
}

function getHeader(
  headers: Record<string, string>,
  name: string
): string | undefined {
  const lowerName = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === lowerName) {
      return value;
    }
  }
  return undefined;
}

function setHeader(
  headers: Record<string, string>,
  name: string,
  value: string
): void {
  const lowerName = name.toLowerCase();
  const existing = Object.keys(headers).find(
    (key) => key.toLowerCase() === lowerName
  );
  headers[existing ?? name] = value;
}

function canSetCookieHeader(): boolean {
  return typeof window === "undefined";
}

type RawAuthKeysResult = Omit<AuthKeysResult, "key_id"> & {
  key_id?: string;
  id?: string;
};

export class BandSox {
  private baseUrl: string;
  private headers: Record<string, string>;
  private timeout: number;
  private wsCtor?: typeof WebSocket;
  private sessionToken?: string;

  constructor(config: BandSoxConfig) {
    this.baseUrl = config.baseUrl.replace(/\/+$/, "");
    this.headers = config.headers ?? {};
    this.timeout = config.timeout ?? 60_000;
    this.wsCtor = config.WebSocket;
  }

  private async request<T>(
    method: string,
    path: string,
    options: {
      json?: unknown;
      params?: Record<string, string | null>;
      timeout?: number;
      formData?: FormData;
      rawResponse?: boolean;
    } = {}
  ): Promise<T> {
    const url = new URL(`${this.baseUrl}${path}`);
    if (options.params) {
      for (const [key, value] of Object.entries(options.params)) {
        if (value != null) {
          url.searchParams.set(key, value);
        }
      }
    }

    const init: RequestInit = {
      method,
      headers: this.requestHeaders(),
      credentials: "include",
    };

    if (options.json !== undefined) {
      init.headers = {
        ...init.headers,
        "Content-Type": "application/json",
      };
      init.body = JSON.stringify(
        stripNulls(options.json as Record<string, unknown>)
      );
    }

    if (options.formData) {
      init.body = options.formData;
    }

    const signal = AbortSignal.timeout(options.timeout ?? this.timeout);
    const resp = await fetch(url.toString(), { ...init, signal });

    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        if (body.detail) {
          detail = String(body.detail);
        }
      } catch {
        // use status text
      }
      throw new BandSoxError(detail, resp.status);
    }

    if (options.rawResponse) {
      return resp as unknown as T;
    }

    const contentType = resp.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      return resp.json() as Promise<T>;
    }
    return resp.arrayBuffer() as Promise<T>;
  }

  // ─── VM creation ───

  async createVm(options: CreateVmOptions): Promise<MicroVM> {
    const payload = {
      image: options.image,
      name: options.name ?? null,
      vcpu: options.vcpu ?? null,
      mem_mib: options.mem_mib ?? null,
      enable_networking: options.enable_networking ?? null,
      force_rebuild: options.force_rebuild ?? null,
      disk_size_mib: options.disk_size_mib ?? null,
      env_vars: options.env_vars ?? null,
      metadata: options.metadata ?? null,
    };
    const res = await this.request<{ id: string }>("POST", "/api/vms", {
      json: payload,
    });
    return new MicroVM(res.id, this);
  }

  async createVmFromDockerfile(
    dockerfile: string,
    options: CreateVmFromDockerfileOptions = {}
  ): Promise<MicroVM> {
    const formData = new FormData();
    formData.append(
      "dockerfile",
      new Blob([dockerfile], { type: "text/plain" }),
      "Dockerfile"
    );

    const fields: Record<string, string | undefined> = {
      vcpu: options.vcpu?.toString(),
      mem_mib: options.mem_mib?.toString(),
      disk_size_mib: options.disk_size_mib?.toString(),
      force_rebuild: options.force_rebuild?.toString(),
      tag: options.tag ?? undefined,
      name: options.name ?? undefined,
    };
    if (options.env_vars) {
      fields["env_vars"] = JSON.stringify(options.env_vars);
    }
    if (options.metadata) {
      fields["metadata"] = JSON.stringify(options.metadata);
    }

    for (const [key, value] of Object.entries(fields)) {
      if (value !== undefined) {
        formData.append(key, value);
      }
    }

    const res = await this.request<{ id: string }>(
      "POST",
      "/api/vms/from-dockerfile",
      { formData, timeout: this.timeout * 3 }
    );
    return new MicroVM(res.id, this);
  }

  async restoreVm(
    snapshotId: string,
    options: RestoreVmOptions = {}
  ): Promise<MicroVM> {
    const payload = {
      name: options.name ?? null,
      enable_networking: options.enable_networking ?? null,
      env_vars: options.env_vars ?? null,
      metadata: options.metadata ?? null,
    };
    const res = await this.request<{ id: string }>(
      "POST",
      `/api/snapshots/${snapshotId}/restore`,
      { json: payload }
    );
    return new MicroVM(res.id, this);
  }

  // ─── VM queries ───

  async listProjects(options: ListVmsOptions = {}): Promise<VmInfo[]> {
    const params: Record<string, string | null> = {};
    if (options.limit != null) {
      params["limit"] = String(options.limit);
    }
    if (options.metadata_equals) {
      params["metadata_equals"] = JSON.stringify(options.metadata_equals);
    }
    return this.request<VmInfo[]>("GET", "/api/projects", { params });
  }

  async listVms(options: ListVmsOptions = {}): Promise<VmInfo[]> {
    const params: Record<string, string | null> = {};
    if (options.limit != null) {
      params["limit"] = String(options.limit);
    }
    if (options.metadata_equals) {
      params["metadata_equals"] = JSON.stringify(options.metadata_equals);
    }
    return this.request<VmInfo[]>("GET", "/api/vms", { params });
  }

  async getVmInfo(vmId: string): Promise<VmInfo | null> {
    try {
      return await this.request<VmInfo>("GET", `/api/vms/${vmId}`);
    } catch (err) {
      if (err instanceof BandSoxError && err.statusCode === 404) {
        return null;
      }
      throw err;
    }
  }

  async getVm(vmId: string): Promise<MicroVM | null> {
    const info = await this.getVmInfo(vmId);
    if (!info) return null;
    return new MicroVM(vmId, this, info);
  }

  // ─── VM lifecycle ───

  async stopVm(vmId: string): Promise<void> {
    await this.request("POST", `/api/vms/${vmId}/stop`);
  }

  async pauseVm(vmId: string): Promise<void> {
    await this.request("POST", `/api/vms/${vmId}/pause`);
  }

  async resumeVm(vmId: string): Promise<void> {
    await this.request("POST", `/api/vms/${vmId}/resume`);
  }

  async deleteVm(vmId: string): Promise<void> {
    await this.request("DELETE", `/api/vms/${vmId}`);
  }

  // ─── VM metadata ───

  async renameVm(vmId: string, newName: string): Promise<void> {
    await this.request("PUT", `/api/vms/${vmId}/name`, {
      json: { name: newName },
    });
  }

  async updateVmMetadata(
    vmId: string,
    metadata: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    return this.request("PUT", `/api/vms/${vmId}/metadata`, {
      json: { metadata },
    });
  }

  // ─── Snapshots ───

  async snapshotVm(
    vmId: string,
    options: SnapshotOptions
  ): Promise<string> {
    const payload = {
      name: options.name,
      metadata: options.metadata ?? null,
    };
    const res = await this.request<{ snapshot_id: string }>(
      "POST",
      `/api/vms/${vmId}/snapshot`,
      { json: payload }
    );
    return res.snapshot_id;
  }

  async listSnapshots(): Promise<SnapshotInfo[]> {
    return this.request<SnapshotInfo[]>("GET", "/api/snapshots");
  }

  async deleteSnapshot(snapshotId: string): Promise<void> {
    await this.request("DELETE", `/api/snapshots/${snapshotId}`);
  }

  async updateSnapshotMetadata(
    snapshotId: string,
    metadata: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    return this.request("PUT", `/api/snapshots/${snapshotId}/metadata`, {
      json: { metadata },
    });
  }

  async renameSnapshot(
    snapshotId: string,
    newName: string
  ): Promise<void> {
    await this.request("PUT", `/api/snapshots/${snapshotId}/name`, {
      json: { name: newName },
    });
  }

  // ─── Auth ───

  private requestHeaders(): Record<string, string> {
    const headers = { ...this.headers };
    if (this.sessionToken && canSetCookieHeader()) {
      const cookie = getHeader(headers, "cookie");
      if (cookie) {
        setHeader(
          headers,
          "Cookie",
          `${cookie}; ${SESSION_COOKIE_NAME}=${this.sessionToken}`
        );
      } else {
        setHeader(headers, "Cookie", `${SESSION_COOKIE_NAME}=${this.sessionToken}`);
      }
    }
    return headers;
  }

  async authCheck(): Promise<AuthCheckResult> {
    return this.request("GET", "/api/auth/check");
  }

  async login(password: string): Promise<{ status: string; token: string }> {
    const result = await this.request<{ status: string; token: string }>(
      "POST",
      "/api/auth/login",
      { json: { password } }
    );
    this.sessionToken = result.token;
    return result;
  }

  async logout(): Promise<{ status: string }> {
    const result = await this.request<{ status: string }>(
      "POST",
      "/api/auth/logout"
    );
    this.sessionToken = undefined;
    return result;
  }

  async listApiKeys(): Promise<AuthKeysResult[]> {
    const keys = await this.request<RawAuthKeysResult[]>("GET", "/api/auth/keys");
    return keys.map(({ id, key_id, ...key }) => ({
      ...key,
      key_id: key_id ?? id!,
    }));
  }

  async createApiKey(name: string): Promise<CreateApiKeyResult> {
    return this.request("POST", "/api/auth/keys", {
      json: { name },
    });
  }

  async revokeApiKey(keyId: string): Promise<{ status: string }> {
    return this.request("DELETE", `/api/auth/keys/${keyId}`);
  }

  // ─── Terminal ───

  connectTerminal(vmId: string, cols = 80, rows = 24): TerminalSession {
    const Ws = this.wsCtor ?? globalThis.WebSocket;
    if (!Ws) {
      throw new Error(
        "WebSocket is not available. Provide a WebSocket constructor in BandSoxConfig.WebSocket."
      );
    }
    const wsUrl = this.baseUrl.replace(/^http/, "ws");
    const raw = getHeader(this.headers, "authorization") ?? "";
    const token = raw.replace(/^Bearer\s+/i, "") || this.sessionToken;
    const url = new URL(`${wsUrl}/api/vms/${vmId}/terminal`);
    url.searchParams.set("cols", String(cols));
    url.searchParams.set("rows", String(rows));
    if (token) {
      url.searchParams.set("token", token);
    }
    return new TerminalSession(new Ws(url.toString()));
  }

  // ─── Internal: exposed for MicroVM ───

  /** @internal */
  _request<T>(
    method: string,
    path: string,
    options?: {
      json?: unknown;
      params?: Record<string, string | null>;
      timeout?: number;
      formData?: FormData;
    }
  ): Promise<T> {
    return this.request<T>(method, path, options);
  }

  /** @internal */
  _rawRequest(
    method: string,
    path: string,
    options?: {
      params?: Record<string, string | null>;
      timeout?: number;
    }
  ): Promise<Response> {
    return this.request<Response>(method, path, {
      ...options,
      rawResponse: true,
    });
  }
}
