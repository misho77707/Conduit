// Typed client for the Conduit API. Every shape here mirrors backend/app/schemas.py and the
// job engine's dict responses (backend/app/api/jobs.py, backend/app/engine/runner.py) exactly.

export class ApiError extends Error {
  status: number;
  code: string | null; // a permission error's code: "forbidden" | "password_change_required"
  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
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
// list of {msg, loc, ...} objects, not the plain string our own HTTPException(...) calls use. Permission errors
// (backend/app/deps.py) are {code, message}.
function detailMessage(body: unknown): string | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) return String((detail as { message: unknown }).message);
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => {
      const msg = d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : String(d);
      return msg.replace(/^Value error,\s*/, "");
    });
    return msgs.join("; ") || null;
  }
  return null;
}

function detailCode(body: unknown): string | null {
  const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
  return detail && typeof detail === "object" && "code" in detail ? String((detail as { code: unknown }).code) : null;
}

// The API ended the session (401: password or role changed, deactivated, deleted), or wants a new password before
// anything else (403 password_change_required). lib/auth.tsx listens and routes accordingly.
export type SessionEvent = "signed_out" | "password_change_required";
const sessionListeners = new Set<(e: SessionEvent) => void>();
/** A 401 means the session ended: lib/auth.tsx already says so, so screens don't add their own error toast. */
export const sessionEnded = (e: unknown) => e instanceof ApiError && e.status === 401;

export function onSessionEvent(fn: (e: SessionEvent) => void): () => void {
  sessionListeners.add(fn);
  return () => { sessionListeners.delete(fn); };
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
    const code = detailCode(body);
    if (res.status === 401 && token && path !== "/auth/login") {
      setToken(null);
      sessionListeners.forEach((fn) => fn("signed_out"));
    } else if (res.status === 403 && code === "password_change_required") {
      sessionListeners.forEach((fn) => fn("password_change_required"));
    }
    throw new ApiError(res.status, detailMessage(body) || res.statusText || `Request failed (${res.status})`, code);
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

export type Role = "superadmin" | "admin" | "viewer";
export interface TokenOut { access_token: string; token_type: string; }
export interface UserOut { id: string; username: string; role: Role; must_change_password: boolean; }

export const auth = {
  login: (username: string, password: string) => post<TokenOut>("/auth/login", { username, password }),
  me: () => get<UserOut>("/auth/me"),
  // every role; ends your other sessions and returns a fresh token for this one
  changePassword: (current_password: string, new_password: string) =>
    post<TokenOut>("/auth/password", { current_password, new_password }),
};

// ---- users (superadmins only): backend/app/api/users.py ----------------------------------------------------

export interface UserAdminOut {
  id: string; username: string; role: Role; is_active: boolean; must_change_password: boolean;
  created_at: string; created_by: string | null; last_login_at: string | null;
}
export const usersApi = {
  list: () => get<UserAdminOut[]>("/users"),
  create: (body: { username: string; role: Role; temporary_password: string }) => post<UserAdminOut>("/users", body),
  update: (id: string, body: { role?: Role; is_active?: boolean }) => patch<UserAdminOut>(`/users/${id}`, body),
  resetPassword: (id: string, temporary_password: string) =>
    post<UserAdminOut>(`/users/${id}/reset-password`, { temporary_password }),
  remove: (id: string) => del<void>(`/users/${id}`),
};

// ---- meta / connections -------------------------------------------------------------

export type ConnectionCategory = "database" | "storage";
export interface DbTypeOut {
  type: string; label: string; abbr: string; kind: "host" | "uri" | "file" | "s3" | "sftp" | "http";
  default_port: number | null; description: string; secret_fields: string[];
  category: ConnectionCategory; // storage = S3 / SFTP / HTTP: files, never tables
}

export interface ConnectionOut {
  id: string; name: string; type: string; env: string | null; host: string; category: ConnectionCategory; database: string | null;
  config: Record<string, unknown>; status: "ok" | "warn" | "err" | "idle"; latency_ms: number | null;
  server_version: string | null; note: string | null; last_checked_at: string | null; created_at: string;
  needs_credentials: boolean; // imported without its secrets: unusable until edited with them
}

export interface TestResultOut {
  ok: boolean; status: "ok" | "warn" | "err"; latency_ms: number | null; version: string | null;
  detail: string | null; error: string | null; degraded: boolean;
  learned?: Record<string, unknown>; // config a test discovered (an SFTP host key): saved with the connection
}
export interface ConnectionTestOut extends TestResultOut { connection: ConnectionOut | null; }

/** Connections that hold tables (everything except file storage), for every database picker. */
export const databasesOnly = (conns: ConnectionOut[]) => conns.filter((c) => c.category !== "storage");

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
  path: string; format: string; compression?: "gzip" | "zip" | null; columns: ColumnOut[]; row_estimate: number | null; exact: boolean;
  size_bytes: number | null; preview: Record<string, unknown>[]; notes: string[];
}
export interface FileEntry { name: string; path: string; is_dir: boolean; size_bytes: number | null; modified_at: string | null; format: string | null; compression?: "gzip" | "zip" | null; }

export const filesApi = {
  inspect: (path: string, sampleRows = 20, opts?: { header?: boolean; skip_rows?: number; delimiter?: string | null }) =>
    post<FileInspectOut>("/files/inspect", { path, sample_rows: sampleRows, ...(opts || {}) }),
  list: (path = "") => get<FileEntry[]>(`/files/list${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  roots: () => get<string[]>("/files/roots"),
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
  remote?: RemoteIn | null;
  combined?: CombinedIn | null; // pipelines only
}

// ---- combined sources (pipelines only): backend/app/engine/spec.py CombinedIn ------------------------------

export interface CombinedInputIn {
  alias: string; // the table name the SQL uses for this input
  source: SourceIn; // a table / query / file / remote file; its `where` is pushed down
  columns?: string[] | null; // read only these columns (null = all)
}
export interface CombinedWatermarkIn {
  column: string; // a column of the driver
  overlap: number; // re-read this much below the saved value: seconds for date/time, else the column's units
}
export interface CombinedIn {
  inputs: CombinedInputIn[]; // 2-8
  sql: string; // one DuckDB SELECT over the aliases
  mode: "staged" | "stream";
  driver?: string | null; // an alias: streamed in stream mode, the watermark's input
  watermark?: CombinedWatermarkIn | null;
}
/** What a pipeline preview reports about a combined source (backend/app/engine/combined.py preview). */
export interface CombinedPreviewInfo {
  mode: "staged" | "stream";
  inputs: { alias: string; rows: number; capped: boolean; columns: { name: string; type: string }[] }[];
  join_rows: number; // rows the SQL returned over the previewed inputs
  notes: string[];
  key_mismatches: string[]; // join conditions over columns of different kinds
}

// ---- remote files (S3 / SFTP / HTTP): backend/app/engine/spec.py RemoteIn, backend/app/api/remote.py ----------

export type RemoteMatch = "exact" | "newest" | "all";
export interface RemoteIn {
  connection_id: string | null; // an S3 / SFTP / HTTP connection; null = an ad-hoc public http(s) URL
  path: string; // key / path / URL / pattern, relative to the connection's base
  match: RemoteMatch;
  skip_loaded: boolean; // newest / all: leave out files already loaded into the same target
  format?: "csv" | "json" | "parquet" | "excel" | null; delimiter?: string | null; header?: boolean; encoding?: string;
  sheet?: string | null; skip_rows?: number; null_values?: string[];
}
export interface RemoteEntryOut {
  name: string; path: string; is_dir: boolean; size: number | null; modified: string | null;
  format: string | null; compression: "gzip" | "zip" | null;
}
export interface RemoteMatchFile {
  name: string; path: string; size: number | null; modified: string | null; format: string | null;
  compression: "gzip" | "zip" | null; status: "new" | "changed" | "loaded" | null;
}
export interface RemoteMatchOut {
  pattern: string; match: RemoteMatch; total: number; total_bytes: number; files: RemoteMatchFile[];
  chosen: string[]; chosen_bytes: number; new_count: number; changed_count: number; loaded_count: number;
  known_status: boolean; skip: boolean; more_left: number; unsupported: number; notes: string[];
}
export interface RemoteTargetRef { connection_id: string; table: string; mode: "append" | "replace" | "upsert"; }
export interface RemoteInspectOut extends FileInspectOut {
  remote: {
    file: { name: string; path: string; display: string; size: number | null; modified: string | null; sampled: boolean };
    match: RemoteMatchOut;
  };
}
export const remoteApi = {
  list: (connectionId: string, path = "") =>
    get<RemoteEntryOut[]>(`/remote/${connectionId}/list${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  match: (remote: RemoteIn, target?: RemoteTargetRef | null) => post<RemoteMatchOut>("/remote/match", { remote, target: target ?? null }),
  inspect: (remote: RemoteIn, sampleRows = 20, target?: RemoteTargetRef | null) =>
    post<RemoteInspectOut>("/remote/inspect", { remote, sample_rows: sampleRows, target: target ?? null }),
};
export interface TargetIn {
  connection_id: string; table: string; mode: "append" | "replace" | "upsert"; keys: string[];
  create: boolean; order_by: string[]; partition_by?: string | null;
  ddl?: string | null; // hand-edited CREATE statements, run instead of the generated DDL for a new table
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
  phase: string | null; // a combined source: "staging …" / "joining" / "loading", then the final status
  trigger?: "manual" | "schedule"; // Jobs list and detail
  started_by?: string | null; // "schedule", a username, or null (a deleted user)
}
export interface JobSplitOut {
  idx: number; label: string; status: string; est_rows: number | null; rows_written: number;
  rows_rejected: number; attempts: number; error: string | null;
}
export interface JobLogLine { t: number; level: string; msg: string; }
export interface JobDetail extends JobSnapshot {
  splits: JobSplitOut[]; log: JobLogLine[];
  plan: JobPlan | Record<string, unknown> | null; // a viewer gets only {notes, warnings}
  spec: JobIn | null; // the request the job was created from (null for a viewer)
  source_label: string; target_label: string; // e.g. "orders (prod-pg)" and "public.orders (ch)"
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

// ---- compare ------------------------------------------------------------------------
// backend/app/compare/spec.py and the dicts of backend/app/compare/engine.py (snapshot, describe, summary)

export type CompareSet = "missing" | "extra" | "mismatched" | "dup_source" | "dup_target";
export interface ComparePair { source: string; target: string; include: boolean; }
export interface CompareOptions {
  ignore_nulls: boolean; check_duplicates: boolean; trim_text: boolean; case_insensitive: boolean;
  float_decimals: number; sample_rows: number;
}
export interface CompareIn {
  name?: string | null; source: SourceIn; target: SourceIn;
  columns?: ComparePair[] | null; // null = pair same-named columns
  keys?: string[] | null; // null = target's primary key; [] = compare whole rows
  options: CompareOptions; job_id?: string | null;
}
export interface CompareColumnsOut {
  source_columns: { name: string; type: string }[]; target_columns: { name: string; type: string }[];
  pairs: { source: string; target: string }[]; unpaired_source: string[]; unpaired_target: string[];
  suggested_keys: string[]; key_note: string;
}
export interface CompareSummary {
  mode: "key" | "row"; keys: string[]; key_note: string;
  columns: { name: string; source: string; source_type: string; target_type: string; compared_as: string }[];
  unpaired_source: string[]; unpaired_target: string[];
  rows_source: number; rows_target: number; matched: number; missing: number; extra: number; mismatched: number;
  dup_source_rows: number; dup_target_rows: number; dup_source_keys?: number; dup_target_keys?: number;
  null_keys_source?: number; null_keys_target?: number;
  column_mismatches: Record<string, number>; sets: Partial<Record<CompareSet, number>>;
  notes: string[]; identical: boolean; seconds: number; options: CompareOptions;
}
export interface CompareOut {
  id: string; name: string; status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "stalled"; phase: string;
  rows_source: number; rows_target: number; summary: CompareSummary | null; error: string | null;
  spec: CompareIn | null; // null for a viewer
  source_label: string; target_label: string;
  job_id: string | null; created_at: string; started_at: string | null; finished_at: string | null;
}
export interface CompareRowsOut { set: CompareSet; columns: string[]; rows: Record<string, unknown>[]; total: number; offset: number; limit: number; }
export interface CompareExportParams { sets: CompareSet[]; columns: "all" | "keys" | string[]; format: "csv" | "xlsx" | "parquet" | "json"; rows: "all" | "sample"; }

export const compareApi = {
  columns: (source: SourceIn, target: SourceIn) => post<CompareColumnsOut>("/compare/columns", { source, target }),
  create: (body: CompareIn) => post<CompareOut>("/compare", body),
  list: () => get<CompareOut[]>("/compare"),
  get: (id: string) => get<CompareOut>(`/compare/${id}`),
  rows: (id: string, set: CompareSet, offset = 0, limit = 100) =>
    get<CompareRowsOut>(`/compare/${id}/rows?set=${set}&offset=${offset}&limit=${limit}`),
  cancel: (id: string) => post<CompareOut>(`/compare/${id}/cancel`),
  remove: (id: string) => del<void>(`/compare/${id}`),
  download: async (id: string, p: CompareExportParams): Promise<{ blob: Blob; filename: string }> => {
    const qs = new URLSearchParams({
      sets: p.sets.join(","), columns: Array.isArray(p.columns) ? p.columns.join(",") : p.columns, format: p.format, rows: p.rows,
    });
    const token = getToken();
    const res = await fetch(`/api/compare/${id}/export?${qs}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = detailMessage(await res.json()) || msg; } catch { /* not JSON */ }
      throw new ApiError(res.status, msg);
    }
    const cd = res.headers.get("content-disposition") || "";
    const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(cd)?.[1];
    const plain = /filename="?([^";]+)"?/i.exec(cd)?.[1];
    return { blob: await res.blob(), filename: star ? decodeURIComponent(star) : plain || `comparison.${p.format}` };
  },
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

/** What every role sees of a connection on the Monitor; admins also get the rest of ConnectionOut. */
export type ConnectionHealth = Pick<ConnectionOut, "id" | "name" | "type" | "status" | "latency_ms" | "last_checked_at" | "note">
  & Partial<ConnectionOut>;

export interface MonitorSnapshot {
  updated_at: string;
  connections: ConnectionHealth[];
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
  // the scheduler's last look at a remote newest / all source that had nothing new (no job is created then)
  last_checked_at: string | null; last_check_note: string | null;
  source_columns: StepOutCol[] | null; // a combined source's join schema, from the last full preview
}
/** GET /pipelines (every role): the list's shape; the source, steps and target come with GET /pipelines/{id}. */
export interface PipelineBriefOut {
  id: string; name: string; description: string;
  source_kind: "table" | "query" | "file" | "remote" | "combined";
  source_label: string; target_label: string; step_count: number;
  runnable: boolean; // a full preview cached the schema that /run and the scheduler need
  schedule: string | null; enabled: boolean; next_run_at: string | null; last_run: PipelineLastRun | null;
  last_checked_at: string | null; last_check_note: string | null;
  created_by: string | null; created_at: string; updated_at: string;
}
export interface PipelinePreviewOut {
  columns: StepOutCol[]; rows: Record<string, unknown>[]; sampled_rows: number; full_chain: boolean;
  // set when a step failed: columns/rows are then the output of the step before it
  error: string | null; error_step: string | null;
  combined?: CombinedPreviewInfo | null; // combined sources only
  source_columns?: StepOutCol[] | null; // combined sources: the join SQL's exact output schema
}

export const pipelinesApi = {
  list: () => get<PipelineBriefOut[]>("/pipelines"),
  create: (body: PipelineIn) => post<PipelineOut>("/pipelines", body),
  get: (id: string) => get<PipelineOut>(`/pipelines/${id}`),
  update: (id: string, body: PipelinePatch) => patch<PipelineOut>(`/pipelines/${id}`, body),
  remove: (id: string) => del<void>(`/pipelines/${id}`),
  preview: (id: string, sampleRows = 50, uptoStep?: string | null) =>
    post<PipelinePreviewOut>(`/pipelines/${id}/preview`, { sample_rows: sampleRows, upto_step: uptoStep ?? null }),
  run: (id: string) => post<JobSnapshot>(`/pipelines/${id}/run`),
};

// ---- alerts (Control & Setup): backend/app/api/alerts.py, app/alerts/catalog.py ------------------------------

export type AlertKind = "telegram" | "slack" | "email" | "webhook";
export type AlertEvent = "job_failed" | "job_stalled" | "schedule_failed" | "input_stale" | "connection_down" | "check_down"
  | "host_high" | "compare_diff";

/** A channel's non-secret settings. Secrets (bot_token, webhook_url, password, url / headers / signing_secret) are
 * sent on save only and never come back: a blank one on edit keeps the stored value. */
export interface AlertChannelIn {
  name: string; kind: AlertKind; enabled?: boolean;
  config: Record<string, unknown>; // telegram: chat_id; email: host, port, tls, user, from, to
  secrets: Record<string, unknown>;
}
export interface AlertChannelOut {
  id: string; name: string; kind: AlertKind; config: Record<string, unknown>;
  secrets_set: string[]; // which secrets are stored, never their values
  enabled: boolean; last_sent_at: string | null; last_error: string | null;
  rules: string[]; // names of the rules that send here
  created_at: string; updated_at: string;
}
export interface AlertScope { all?: boolean; pipelines?: string[]; connections?: string[]; envs?: string[]; checks?: string[]; }
export interface AlertRuleIn {
  name: string; event: AlertEvent; scope: AlertScope; options: Record<string, unknown>; channel_ids: string[]; enabled?: boolean;
}
export interface AlertRuleOut {
  id: string; name: string; event: AlertEvent; event_label: string; scope: AlertScope; options: Record<string, unknown>;
  channel_ids: string[]; enabled: boolean; firing: number; created_at: string; updated_at: string;
}
export interface AlertSendResult { channel_id: string | null; name: string; ok: boolean; error: string | null; }
export interface ActiveAlert {
  rule_id: string | null; rule_name: string; event: AlertEvent; event_label: string; // ids: null for a viewer
  subject_kind: string; subject_id: string | null; subject_label: string;
  since: string | null; fired_at: string | null; reason: string; link: string; // link: an app page, "#/jobs/<id>"
}
export interface ActiveAlertsOut { muted_until: string | null; alerts: ActiveAlert[]; }

export const alertsApi = {
  channels: () => get<AlertChannelOut[]>("/alerts/channels"),
  createChannel: (body: AlertChannelIn) => post<AlertChannelOut>("/alerts/channels", body),
  updateChannel: (id: string, body: Partial<AlertChannelIn> & { clear_secrets?: string[] }) =>
    patch<AlertChannelOut>(`/alerts/channels/${id}`, body),
  removeChannel: (id: string) => del<void>(`/alerts/channels/${id}`),
  testChannel: (id: string) => post<AlertSendResult>(`/alerts/channels/${id}/test`),
  testUnsaved: (body: AlertChannelIn & { id?: string | null }) => post<AlertSendResult>("/alerts/channels/test", body),
  rules: () => get<AlertRuleOut[]>("/alerts/rules"),
  createRule: (body: AlertRuleIn) => post<AlertRuleOut>("/alerts/rules", body),
  updateRule: (id: string, body: Partial<AlertRuleIn>) => patch<AlertRuleOut>(`/alerts/rules/${id}`, body),
  removeRule: (id: string) => del<void>(`/alerts/rules/${id}`),
  sendTest: (channel_ids: string[], rule_name?: string) => post<AlertSendResult[]>("/alerts/test", { channel_ids, rule_name: rule_name || null }),
  mute: () => get<{ until: string | null }>("/alerts/mute"),
  setMute: (until: string | null) => req<{ until: string | null }>("/alerts/mute", { method: "PUT", body: JSON.stringify({ until }) }),
  active: () => get<ActiveAlertsOut>("/alerts/active"), // every role (viewers: no ids)
};

// ---- settings (Control & Setup; admins) ----

export interface SettingOut {
  key: string;
  env: string; // the .env variable, e.g. CONDUIT_KEEP_FINISHED_JOBS
  group: string;
  group_label: string;
  label: string;
  help: string;
  cls: "runtime" | "restart"; // runtime: editable here; restart: set in .env, then restart
  type: "int" | "float" | "text";
  unit: string;
  min: number | null;
  max: number | null;
  value: number | string; // the effective value
  default: number | string;
  env_value: number | string | null; // what .env sets, when it sets something other than the default
  source: "ui" | "env" | "default";
  note: string | null;
  updated_by: string | null; // a UI override: who and when
  updated_at: string | null;
}

export const settingsApi = {
  list: () => get<SettingOut[]>("/settings"),
  set: (key: string, value: number | string) => req<SettingOut>(`/settings/${key}`, { method: "PUT", body: JSON.stringify({ value }) }),
  reset: (key: string) => del<SettingOut>(`/settings/${key}`),
};

// ---- export / import (Control & Setup; admins) ----

export type ImportSection = "settings" | "connections" | "service_checks" | "pipelines" | "alert_channels" | "alert_rules";
export interface ImportItem {
  key: string; // what apply picks
  section: ImportSection;
  name: string;
  action: "create" | "update" | "skip" | "conflict";
  notes: string[];
}
export interface ImportPlan {
  instance: string; // where the file came from
  exported_at: string | null;
  version: number;
  secrets: string; // none | encrypted
  secrets_used: boolean; // a passphrase opened them
  notes: string[];
  items: ImportItem[];
}
export interface ImportResult { key: string; section: string; name: string; action: string; ok: boolean; error: string | null; }

export const transferApi = {
  /** The export file (secrets only for a superadmin, under a passphrase). */
  exportFile: async (include_secrets: boolean, passphrase: string | null): Promise<{ blob: Blob; filename: string }> => {
    const token = getToken();
    const res = await fetch("/api/export", {
      method: "POST", body: JSON.stringify({ include_secrets, passphrase }),
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = detailMessage(await res.json()) || msg; } catch { /* not JSON */ }
      throw new ApiError(res.status, msg);
    }
    const plain = /filename="?([^";]+)"?/i.exec(res.headers.get("content-disposition") || "")?.[1];
    return { blob: await res.blob(), filename: plain || "conduit-export.json" };
  },
  plan: (document: unknown, passphrase: string | null) => post<ImportPlan>("/import/plan", { document, passphrase }),
  apply: (document: unknown, passphrase: string | null, picked: string[]) =>
    post<ImportResult[]>("/import/apply", { document, passphrase, picked }),
};
