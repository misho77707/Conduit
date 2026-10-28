import { NO_MOVEMENT, type Movement } from "../jobs/RowMovement";
import type { ColumnOut, ConnectionOut, FileEntry, FileInspectOut, JobPlan, RemoteIn, RemoteMatchOut, TableOut, TableSchemaOut } from "../../lib/api";

export type SourceTab = "upload" | "server" | "remote" | "database";
export type Strategy = "full" | "id" | "time" | "watermark";
export type Mode = "append" | "replace" | "upsert";

export interface ColumnMapUI {
  source: string;
  name: string;
  type: string; // AUTO_TYPE or a LOGICAL_TYPES member
  include: boolean;
  nativeType: string; // display only
}

export interface WizardState {
  step: 1 | 2 | 3 | 4;
  sourceTab: SourceTab;

  // remote file source (S3 / SFTP / URL), once inspected; fileInspect / fileHeader / fileSkipRows are shared with local files
  remote: RemoteSource | null;

  // file source
  filePath: string | null;
  fileInspect: FileInspectOut | null;
  fileHeader: boolean;
  fileSkipRows: number;
  uploadState: "idle" | "uploading" | "done";
  uploadPct: number;
  uploadedName: string | null;
  serverPath: string;
  serverEntries: FileEntry[] | null;

  // database source
  sourceConnId: string | null;
  sourceView: "table" | "query";
  sourceTable: string | null;
  sourceQuery: string;
  tables: TableOut[] | null;
  tableSchema: TableSchemaOut | null;

  // shared columns (post source-inspect)
  columns: ColumnMapUI[];

  // transfer options
  strategy: Strategy;
  splitColumn: string | null; // for id/time; also used for full-mode ctid/partition split
  fullSplitColumn: string | null; // split column while strategy === 'full'
  lo: string;
  hi: string;
  timezone: string;
  afterWatermark: boolean;
  lastNDays: boolean;
  days: string;
  where: string;
  workers: number;
  chunkMode: "rec" | "custom";
  chunk: number;
  movement: Movement; // driver overrides and the transfer method (chosen on the review step)

  // target
  targetConnId: string | null;
  tableMode: "new" | "existing";
  targetTable: string;
  targetTables: TableOut[] | null;
  targetTableSchema: TableSchemaOut | null;
  mode: Mode;
  keys: string[];
  rejectToFile: boolean;
  ddlText: string | null; // hand-edited CREATE statements for a new table; null = use the generated DDL

  // plan
  plan: JobPlan | null;
  planLoading: boolean;
  planError: string | null;
}

export interface RemoteSource {
  spec: RemoteIn; // what the job sends (header / skip_rows are filled in from the wizard when it is built)
  display: string; // the inspected file, e.g. s3://lake/exports/orders.csv (never a URL query string)
  sampled: boolean; // previewed from the head / first rows only
  match: RemoteMatchOut | null;
}

/** Where the load reads from, by the source tab: a local file (upload / server folder), a remote file, or a database. */
export function sourceMode(s: WizardState): "file" | "remote" | "database" {
  return s.sourceTab === "remote" ? "remote" : s.sourceTab === "database" ? "database" : "file";
}

/** A local or a remote file: previewed and loaded through the file reader, never range-split. */
export const isFileLike = (s: WizardState) => sourceMode(s) !== "database";

export function defaultColumnsFromNative(cols: ColumnOut[]): ColumnMapUI[] {
  return cols.map((c) => ({ source: c.name, name: c.name, type: "(auto)", include: true, nativeType: c.type }));
}

export function initialWizardState(targetConnId: string | null): WizardState {
  return {
    step: 1, sourceTab: "upload", remote: null,
    filePath: null, fileInspect: null, fileHeader: true, fileSkipRows: 0,
    uploadState: "idle", uploadPct: 0, uploadedName: null, serverPath: "", serverEntries: null,
    sourceConnId: null, sourceView: "table", sourceTable: null, sourceQuery: "", tables: null, tableSchema: null,
    columns: [],
    strategy: "full", splitColumn: null, fullSplitColumn: null, lo: "", hi: "", timezone: "UTC",
    afterWatermark: false, lastNDays: false, days: "30", where: "",
    workers: 4, chunkMode: "rec", chunk: 50000, movement: NO_MOVEMENT,
    targetConnId, tableMode: "new", targetTable: "", targetTables: null, targetTableSchema: null,
    mode: "append", keys: [], rejectToFile: true, ddlText: null,
    plan: null, planLoading: false, planError: null,
  };
}

export function sourceConn(conns: ConnectionOut[], id: string | null): ConnectionOut | undefined {
  return conns.find((c) => c.id === id);
}
