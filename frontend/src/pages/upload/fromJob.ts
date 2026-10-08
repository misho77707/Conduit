import { AUTO_TYPE } from "../../lib/constants";
import { connectionsApi, filesApi, remoteApi, type ColumnOut, type JobIn } from "../../lib/api";
import { initialWizardState, type ColumnMapUI, type WizardState } from "./types";

/** Wizard state that reproduces an earlier job's request (the inverse of jobBuilder.buildJobIn), for "Edit & re-run". */
export async function stateFromJob(spec: JobIn): Promise<WizardState> {
  const s = initialWizardState(spec.target.connection_id);
  const src = spec.source;
  let native: ColumnOut[] = [];

  if (src.remote) {
    const insp = await remoteApi.inspect(src.remote, 20);
    native = insp.columns;
    Object.assign(s, {
      sourceTab: "remote", fileInspect: insp, fileHeader: src.remote.header ?? true, fileSkipRows: src.remote.skip_rows ?? 0,
      remote: { spec: src.remote, display: insp.remote.file.display, sampled: insp.remote.file.sampled, match: insp.remote.match },
    } satisfies Partial<WizardState>);
  } else if (src.file) {
    const insp = await filesApi.inspect(src.file.path, 20);
    native = insp.columns;
    Object.assign(s, {
      sourceTab: "upload", filePath: src.file.path, fileInspect: insp, fileHeader: src.file.header,
      fileSkipRows: src.file.skip_rows, uploadState: "done", uploadPct: 1,
      uploadedName: src.file.path.split(/[\\/]/).pop() || src.file.path,
    } satisfies Partial<WizardState>);
  } else {
    Object.assign(s, {
      sourceTab: "database", sourceConnId: src.connection_id || null, where: src.where || "",
      sourceView: src.query ? "query" : "table", sourceTable: src.table || null, sourceQuery: src.query || "",
    } satisfies Partial<WizardState>);
    if (src.table && src.connection_id) {
      s.tableSchema = await connectionsApi.inspect(src.connection_id, src.table);
      native = s.tableSchema.columns;
    }
  }

  // Columns: what the source has now, with the job's renames / types / exclusions laid over it.
  const byName = new Map((spec.columns || []).map((c) => [c.source, c]));
  const cols: ColumnMapUI[] = native.map((c) => {
    const m = byName.get(c.name);
    byName.delete(c.name);
    return {
      source: c.name, name: m?.name || c.name, type: m?.type || AUTO_TYPE,
      include: m ? m.include : !spec.columns, nativeType: c.type,
    };
  });
  for (const m of byName.values()) {  // query sources (no inspect) or columns the job named explicitly
    cols.push({ source: m.source, name: m.name || m.source, type: m.type || AUTO_TYPE, include: m.include, nativeType: "" });
  }
  s.columns = cols;

  const o = spec.options;
  s.timezone = o.timezone;
  s.workers = o.workers;
  s.chunk = o.read_chunk;
  s.chunkMode = o.read_chunk === 50000 ? "rec" : "custom";
  s.rejectToFile = o.max_rejects !== 0;
  if (o.strategy === "full") {
    s.fullSplitColumn = o.split_column || null;
  } else {
    s.strategy = o.strategy === "watermark" ? "id" : o.strategy;
    s.splitColumn = o.split_column || null;
    s.afterWatermark = o.strategy === "watermark";
    s.lo = o.lo != null ? String(o.lo) : "";
    s.hi = o.hi != null ? String(o.hi) : "";
  }

  const t = spec.target;
  s.targetTable = t.table;
  s.mode = t.mode;
  s.keys = t.keys;
  // The earlier run usually created the table, so reopen as "existing" when it is there now.
  const tables = await connectionsApi.tables(t.connection_id).catch(() => null);
  const exists = !!tables?.some((x) => x.name === t.table);
  s.tableMode = t.create && !exists ? "new" : "existing";
  s.ddlText = s.tableMode === "new" ? t.ddl || null : null;
  return s;
}
