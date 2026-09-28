import type { ColumnOut, ConnectionOut, FileEntry, FileInspectOut, JobPlan, TableOut, TableSchemaOut } from "../../lib/api";

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

  // target
  targetConnId: string | null;
  tableMode: "new" | "existing";
  targetTable: string;
  targetTables: TableOut[] | null;
  targetTableSchema: TableSchemaOut | null;
  mode: Mode;
  keys: string[];
  rejectToFile: boolean;

  // plan
  plan: JobPlan | null;
  planLoading: boolean;
  planError: string | null;
}

export function defaultColumnsFromNative(cols: ColumnOut[]): ColumnMapUI[] {
  return cols.map((c) => ({ source: c.name, name: c.name, type: "(auto)", include: true, nativeType: c.type }));
}

export function initialWizardState(targetConnId: string | null): WizardState {
  return {
    step: 1, sourceTab: "upload",
    filePath: null, fileInspect: null, fileHeader: true, fileSkipRows: 0,
    uploadState: "idle", uploadPct: 0, uploadedName: null, serverPath: "", serverEntries: null,
    sourceConnId: null, sourceView: "table", sourceTable: null, sourceQuery: "", tables: null, tableSchema: null,
    columns: [],
    strategy: "full", splitColumn: null, fullSplitColumn: null, lo: "", hi: "", timezone: "UTC",
    afterWatermark: false, lastNDays: false, days: "30", where: "",
    workers: 4, chunkMode: "rec", chunk: 50000,
    targetConnId, tableMode: "new", targetTable: "", targetTables: null, targetTableSchema: null,
    mode: "append", keys: [], rejectToFile: true,
    plan: null, planLoading: false, planError: null,
  };
}

export function sourceConn(conns: ConnectionOut[], id: string | null): ConnectionOut | undefined {
  return conns.find((c) => c.id === id);
}
