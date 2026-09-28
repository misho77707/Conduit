// Typed client for the Conduit API. Every shape here mirrors backend/app/schemas.py and the
// job engine's dict responses (backend/app/api/jobs.py, backend/app/engine/runner.py) exactly.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const TOKEN_KEY = "conduit.token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string | null) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

// FastAPI's own request-validation errors (a field failing a pydantic validator) shape `detail` as a
// list of {msg, loc, ...} objects, not the plain string our own HTTPException(...) calls use.
function detailMessage(body: unknown): string | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => {
      const msg = d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : String(d);
      return msg.replace(/^Value error,\s*/, "");
    });
    return msgs.join("; ") || null;
  }
  return null;
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`/api${path}`, { ...init, headers });
  if (res.status === 204) return undefined as T;
  const isJson = res.headers.get("content-type")?.includes("application/json");
  const body = isJson ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    throw new ApiError(res.status, detailMessage(body) || res.statusText || `Request failed (${res.status})`);
  }
  return body as T;
}

function get<T>(path: string) {
  return req<T>(path);
}
function post<T>(path: string, body?: unknown) {
  return req<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
}
function patch<T>(path: string, body: unknown) {
  return req<T>(path, { method: "PATCH", body: JSON.stringify(body) });
}
function del<T>(path: string) {
  return req<T>(path, { method: "DELETE" });
}

// ---- auth -------------------------------------------------------------------------

export interface TokenOut { access_token: string; token_type: string; }
export interface UserOut { id: string; username: string; role: string; }

export const auth = {
  login: (username: string, password: string) => post<TokenOut>("/auth/login", { username, password }),
  me: () => get<UserOut>("/auth/me"),
};

// ---- meta / connections -------------------------------------------------------------

export interface DbTypeOut {
  type: string; label: string; abbr: string; kind: "host" | "uri" | "file";
  default_port: number | null; description: string; secret_fields: string[];
}

export interface ConnectionOut {
  id: string; name: string; type: string; env: string | null; host: string; database: string | null;
  config: Record<string, unknown>; status: "ok" | "warn" | "err" | "idle"; latency_ms: number | null;
  server_version: string | null; note: string | null; last_checked_at: string | null; created_at: string;
}

export interface TestResultOut {
  ok: boolean; status: "ok" | "warn" | "err"; latency_ms: number | null; version: string | null;
  detail: string | null; error: string | null; degraded: boolean;
}
export interface ConnectionTestOut extends TestResultOut { connection: ConnectionOut | null; }

export interface TableOut { name: string; est_rows: number | null; size_bytes: number | null; exact: boolean; info: string; }

export interface ColumnOut { name: string; type: string; nullable: boolean; primary_key: boolean; indexed: boolean; }
export interface SplitCandidateOut {
  column: string; mode: "id_range" | "time_range" | "ctid" | "partition";
  verdict: "recommended" | "alternative" | "not_advised"; reasons: string[]; warnings: string[];
}
export interface TableSchemaOut {
  name: string; columns: ColumnOut[]; row_estimate: number | null; exact: boolean;
  size_bytes: number | null; candidates: SplitCandidateOut[]; notes: string[];
}

export const metaApi = {
  dbTypes: () => get<DbTypeOut[]>("/meta/db-types"),
};

export const connectionsApi = {
  list: () => get<ConnectionOut[]>("/connections"),
  get: (id: string) => get<ConnectionOut>(`/connections/${id}`),
  create: (body: { name: string; type: string; env?: string | null; config: Record<string, unknown>; secrets: Record<string, unknown> }) =>
    post<ConnectionOut>("/connections", body),
  update: (id: string, body: { name?: string; env?: string | null; config?: Record<string, unknown>; secrets?: Record<string, unknown> }) =>
    patch<ConnectionOut>(`/connections/${id}`, body),
  remove: (id: string) => del<void>(`/connections/${id}`),
  testUnsaved: (body: { type: string; config: Record<string, unknown>; secrets: Record<string, unknown> }) =>
    post<TestResultOut>("/connections/test", body),
  testAll: () => post<ConnectionTestOut[]>("/connections/test-all"),
  test: (id: string) => post<ConnectionTestOut>(`/connections/${id}/test`),
  tables: (id: string) => get<TableOut[]>(`/connections/${id}/tables`),
  inspect: (id: string, table: string) => get<TableSchemaOut>(`/connections/${id}/tables/${encodeURIComponent(table)}/inspect`),
};

// ---- files ------------------------------------------------------------------------

export interface FileInspectOut {
  path: string; format: string; columns: ColumnOut[]; row_estimate: number | null; exact: boolean;
  size_bytes: number | null; preview: Record<string, unknown>[]; notes: string[];
}
export interface FileEntry { name: string; path: string; is_dir: boolean; size_bytes: number | null; modified_at: string | null; format: string | null; }

export const filesApi = {
  inspect: (path: string, sampleRows = 20) => post<FileInspectOut>("/files/inspect", { path, sample_rows: sampleRows }),
  list: (path = "") => get<FileEntry[]>(`/files/list${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  upload: (file: File, onProgress?: (pct: number) => void): Promise<{ path: string; name: string; size_bytes: number }> => {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/files/upload");
      const token = getToken();
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else reject(new ApiError(xhr.status, body?.detail || xhr.statusText));
        } catch {
          reject(new ApiError(xhr.status, xhr.statusText));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "Network error during upload"));
      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });
  },
};

// ---- jobs -------------------------------------------------------------------------

export interface ColumnMapIn {
  source: string; name?: string | null; type?: string | null; nullable?: boolean | null;
  include: boolean; format?: string | null;
}
export interface FileSourceIn {
  path: string; format?: "csv" | "json" | "parquet" | "excel" | null; delimiter?: string | null;
  header: boolean; encoding: string; sheet?: string | null; skip_rows: number; null_values: string[];
}
export interface SourceIn {
  connection_id?: string | null; table?: string | null; query?: string | null; where?: string; file?: FileSourceIn | null;
}
export interface TargetIn {
  connection_id: string; table: string; mode: "append" | "replace" | "upsert"; keys: string[];
  create: boolean; order_by: string[]; partition_by?: string | null;
}
export interface OptionsIn {
  strategy: "full" | "id" | "time" | "watermark"; split_column?: string | null; lo?: string | number | null;
  hi?: string | number | null; timezone: string; workers: number; splits?: number | null; split_rows?: number | null;
  read_chunk: number; write_batch: number; max_rejects: number; retries: number;
}
export interface JobIn {
  name?: string | null; source: SourceIn; target: TargetIn; columns?: ColumnMapIn[] | null; options: OptionsIn;
}

export interface JobPlanSample {
  rows: number; seconds: number; rows_per_s: number; rejected: number;
  reject_examples: { reason: string; row: Record<string, unknown> }[];
  preview: Record<string, unknown>[]; est_duration_s: number | null; basis: string;
}
export interface JobPlan {
  source_columns: { name: string; type: string; nullable: boolean; source_name: string }[];
  target_columns: { name: string; type: string; nullable: boolean; source_name: string; format: string | null }[];
  target_exists: boolean; ddl: string[]; rows_total: number | null; rows_total_exact: boolean;
  split_column: string | null; split_unique: boolean; workers: number;
  splits: { idx: number; label: string; est_rows: number | null; kind: string }[];
  notes: string[]; warnings: string[]; sample: JobPlanSample | null;
}

export interface JobSnapshot {
  id: string; name: string; kind: "transfer" | "file_load" | "pipeline"; status: string;
  rows_total: number | null; rows_total_exact: boolean; rows_read: number; rows_written: number; rows_rejected: number;
  progress: number | null; rate_rows_s: number | null; eta_s: number | null; workers: number;
  splits_total: number; splits_done: number; error: string | null;
  created_at: string; started_at: string | null; finished_at: string | null; has_rejects: boolean;
}
export interface JobSplitOut {
  idx: number; label: string; status: string; est_rows: number | null; rows_written: number;
  rows_rejected: number; attempts: number; error: string | null;
}
export interface JobLogLine { t: number; level: string; msg: string; }
export interface JobDetail extends JobSnapshot {
  splits: JobSplitOut[]; plan: JobPlan | Record<string, unknown> | null; log: JobLogLine[];
}

export const jobsApi = {
  plan: (body: JobIn, sampleRows = 2000) => post<JobPlan>(`/jobs/plan?sample_rows=${sampleRows}`, body),
  create: (body: JobIn) => post<JobDetail>("/jobs", body),
  list: () => get<JobSnapshot[]>("/jobs"),
  get: (id: string) => get<JobDetail>(`/jobs/${id}`),
  cancel: (id: string) => post<JobSnapshot>(`/jobs/${id}/cancel`),
  retry: (id: string) => post<JobDetail>(`/jobs/${id}/retry`),
  remove: (id: string) => del<void>(`/jobs/${id}`),
  rejectsUrl: (id: string) => `/api/jobs/${id}/rejects`,
  downloadRejects: async (id: string): Promise<Blob> => {
    const token = getToken();
    const res = await fetch(`/api/jobs/${id}/rejects`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!res.ok) throw new ApiError(res.status, "This job has no rejected rows");
    return res.blob();
  },
  eventsUrl: (id: string) => `/api/jobs/${id}/events?token=${encodeURIComponent(getToken() || "")}`,
};

// ---- monitor ------------------------------------------------------------------------

export type CheckKind = "http" | "tcp" | "query" | "docker";
export type CheckStatus = "pending" | "up" | "warn" | "down";

export interface CheckIn {
  name: string; kind: CheckKind; target?: string; connection_id?: string | null; query?: string | null;
  expect?: string; interval_s?: number; verify_tls?: boolean; enabled?: boolean;
}
export interface CheckPatch {
  name?: string; target?: string; connection_id?: string | null; query?: string | null;
  expect?: string; interval_s?: number; verify_tls?: boolean; enabled?: boolean;
}
export interface CheckOut {
  id: string; name: string; kind: CheckKind; target: string; connection_id: string | null; query: string | null;
  expect: string; interval_s: number; verify_tls: boolean; enabled: boolean; status: CheckStatus;
  message: string; latency_ms: number | null; checked_at: string | null; stale: boolean;
}

export interface HostDisk { label: string; path: string; total_gb: number; used_gb: number; percent: number; level: "ok" | "high"; }
export interface HostSnapshot {
  hostname: string; os: string; vcpus: number; warn_at_pct: number; boot_time: number; sampled_at: number;
  cpu: { percent: number; level: "ok" | "high"; load_avg: number[] | null };
  memory: { total_gb: number; used_gb: number; percent: number; level: "ok" | "high" };
  disks: HostDisk[]; disks_unavailable: string[];
}

export interface MonitorSnapshot {
  updated_at: string;
  connections: ConnectionOut[];
  host: HostSnapshot;
  jobs: { running: number; queued: number; slots_total: number | null; worker_online: boolean | null; active: JobSnapshot[] };
  services: CheckOut[];
  summary: {
    connections: { up: number; warn: number; down: number; unchecked: number; total: number };
    services: { up: number; warn: number; down: number; pending: number; total: number };
  };
}

export const monitorApi = {
  get: () => get<MonitorSnapshot>("/monitor"),
  refresh: () => post<MonitorSnapshot>("/monitor/refresh"),
  listChecks: () => get<CheckOut[]>("/monitor/checks"),
  createCheck: (body: CheckIn) => post<CheckOut>("/monitor/checks", body),
  updateCheck: (id: string, body: CheckPatch) => patch<CheckOut>(`/monitor/checks/${id}`, body),
  deleteCheck: (id: string) => del<void>(`/monitor/checks/${id}`),
  runCheck: (id: string) => post<CheckOut>(`/monitor/checks/${id}/run`),
};

// ---- pipelines --------------------------------------------------------------------

export type StepKind = "rename" | "cast" | "filter" | "derive" | "split" | "sql" | "python";
export interface StepIn { id: string; kind: StepKind; config: Record<string, unknown>; }
export interface StepOutCol { name: string; type: string; nullable: boolean; source_name: string | null; }

export interface PipelineIn {
  name: string; description?: string; source: SourceIn; target: TargetIn; options?: OptionsIn;
  steps?: StepIn[]; schedule?: string | null; enabled?: boolean;
}
export interface PipelinePatch {
  name?: string; description?: string; source?: SourceIn; target?: TargetIn; options?: OptionsIn;
  steps?: StepIn[]; schedule?: string | null; enabled?: boolean;
}
export interface PipelineLastRun {
  job_id: string; status: string; rows_written: number; started_at: string | null; finished_at: string | null; error: string | null;
}
export interface PipelineOut {
  id: string; name: string; description: string; source: SourceIn; target: TargetIn; options: OptionsIn;
  steps: StepIn[]; output_columns: StepOutCol[] | null; schedule: string | null; enabled: boolean;
  next_run_at: string | null; created_at: string; updated_at: string; last_run: PipelineLastRun | null;
}
export interface PipelinePreviewOut {
  columns: StepOutCol[]; rows: Record<string, unknown>[]; sampled_rows: number; full_chain: boolean;
}

export const pipelinesApi = {
  list: () => get<PipelineOut[]>("/pipelines"),
  create: (body: PipelineIn) => post<PipelineOut>("/pipelines", body),
  get: (id: string) => get<PipelineOut>(`/pipelines/${id}`),
  update: (id: string, body: PipelinePatch) => patch<PipelineOut>(`/pipelines/${id}`, body),
  remove: (id: string) => del<void>(`/pipelines/${id}`),
  preview: (id: string, sampleRows = 50, uptoStep?: string | null) =>
    post<PipelinePreviewOut>(`/pipelines/${id}/preview`, { sample_rows: sampleRows, upto_step: uptoStep ?? null }),
  run: (id: string) => post<JobSnapshot>(`/pipelines/${id}/run`),
};
