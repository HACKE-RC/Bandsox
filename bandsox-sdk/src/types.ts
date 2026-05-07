export interface BandSoxConfig {
  baseUrl: string;
  headers?: Record<string, string>;
  timeout?: number;
}

export interface CreateVmOptions {
  image: string;
  name?: string | null;
  vcpu?: number;
  mem_mib?: number;
  enable_networking?: boolean;
  force_rebuild?: boolean;
  disk_size_mib?: number;
  env_vars?: Record<string, string> | null;
  metadata?: Record<string, unknown> | null;
}

export interface CreateVmFromDockerfileOptions {
  tag?: string | null;
  name?: string | null;
  vcpu?: number;
  mem_mib?: number;
  disk_size_mib?: number;
  force_rebuild?: boolean;
  env_vars?: Record<string, string> | null;
  metadata?: Record<string, unknown> | null;
}

export interface RestoreVmOptions {
  name?: string | null;
  enable_networking?: boolean;
  env_vars?: Record<string, string> | null;
  metadata?: Record<string, unknown> | null;
}

export interface ListVmsOptions {
  limit?: number | null;
  metadata_equals?: Record<string, unknown> | null;
}

export interface SnapshotOptions {
  name: string;
  metadata?: Record<string, unknown> | null;
}

export interface ExecResult {
  exit_code: number;
  stdout: string;
  stderr: string;
}

export interface ExecPythonOptions {
  code: string;
  cwd?: string;
  packages?: string[] | null;
  timeout?: number;
  cleanup_venv?: boolean;
}

export interface ExecPythonResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  output: string;
  success: boolean;
  error: string | null;
}

export interface NetworkConfig {
  guest_mac: string;
  guest_ip: string;
  host_ip: string;
  host_mac?: string;
  tap_name: string;
}

export interface VsockConfig {
  enabled: boolean;
  cid: number;
  port: number;
  uds_path: string;
  baked_uds_path: string | null;
  host_uds_path: string;
}

export interface VmInfo {
  id: string;
  name: string | null;
  image: string;
  vcpu: number;
  mem_mib: number;
  disk_size_mib?: number;
  rootfs_path?: string;
  network_config: NetworkConfig | null;
  vsock_config: VsockConfig | null;
  created_at: number;
  status: "running" | "paused" | "stopped";
  pid: number | null;
  agent_ready: boolean;
  env_vars: Record<string, string> | null;
  metadata: Record<string, unknown>;
  restored_from?: string;
}

export interface FileInfo {
  size: number;
  mode?: number;
  mtime?: number;
}

export interface DirEntry {
  name: string;
  path: string;
  size: number;
  is_dir: boolean;
  is_file: boolean;
  mtime: number;
}

export interface ListDirResult {
  path: string;
  files: DirEntry[];
}

export interface WriteFileOptions {
  path: string;
  content: string;
  encoding?: "utf-8" | "base64";
  append?: boolean;
}

export interface HttpProxyOptions {
  port: number;
  path?: string;
  method?: string;
  headers?: Record<string, string> | null;
  body?: string | null;
  json_body?: Record<string, unknown> | null;
  timeout?: number;
}

export interface HttpProxyResult {
  status_code: number;
  headers: Record<string, string>;
  body: string;
}

export interface SnapshotInfo {
  id: string;
  snapshot_name?: string;
  source_vm_id?: string;
  vcpu?: number;
  mem_mib?: number;
  image?: string;
  rootfs_path?: string;
  network_config?: NetworkConfig | null;
  vsock_config?: VsockConfig | null;
  metadata: Record<string, unknown>;
  created_at: number | null;
  path: string;
  status?: string;
}

export interface AuthCheckResult {
  authenticated: boolean;
}

export interface AuthKeysResult {
  key_id: string;
  name: string;
  created_at?: number;
}

export interface CreateApiKeyResult {
  key_id: string;
  key: string;
  name: string;
}

export interface LoginRequest {
  password: string;
}

export interface CreateKeyRequest {
  name: string;
}

export interface UploadFolderFiles {
  [remotePath: string]: string | Uint8Array;
}
