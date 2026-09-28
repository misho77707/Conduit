import { AUTO_TYPE } from "../../lib/constants";
import type { ColumnMapIn, JobIn, OptionsIn, SourceIn, TargetIn } from "../../lib/api";
import type { WizardState } from "./types";

export function buildSource(s: WizardState): SourceIn {
  if (s.filePath) {
    return {
      where: "",
      file: { path: s.filePath, header: s.fileHeader, skip_rows: s.fileSkipRows, encoding: "utf-8", null_values: [""] },
    };
  }
  if (s.sourceView === "query") {
    return { connection_id: s.sourceConnId, query: s.sourceQuery, where: s.where };
  }
  return { connection_id: s.sourceConnId, table: s.sourceTable, where: s.where };
}

export function buildOptions(s: WizardState): OptionsIn {
  const isFile = !!s.filePath;
  let strategy = s.strategy;
  let split_column: string | null = null;
  let lo: string | null = null;
  let hi: string | null = null;

  if (isFile) {
    strategy = "full";
  } else if (s.strategy === "full") {
    split_column = s.fullSplitColumn;
  } else if (s.strategy === "id") {
    split_column = s.splitColumn;
    if (s.afterWatermark) strategy = "watermark";
    else { lo = s.lo || null; hi = s.hi || null; }
  } else if (s.strategy === "time") {
    split_column = s.splitColumn;
    if (s.lastNDays) {
      const to = new Date();
      const from = new Date(to.getTime() - Number(s.days || 0) * 86_400_000);
      lo = from.toISOString().slice(0, 19);
      hi = to.toISOString().slice(0, 19);
    } else {
      lo = s.lo || null; hi = s.hi || null;
    }
  }

  return {
    strategy, split_column, lo, hi, timezone: s.timezone, workers: s.workers,
    splits: null, split_rows: null, read_chunk: s.chunk, write_batch: s.chunk,
    max_rejects: s.rejectToFile ? -1 : 0, retries: 3,
  };
}

export function buildTarget(s: WizardState): TargetIn {
  return {
    connection_id: s.targetConnId!, table: s.targetTable, mode: s.mode,
    keys: s.mode === "upsert" ? s.keys : [], create: s.tableMode === "new", order_by: [], partition_by: null,
  };
}

export function buildColumns(s: WizardState): ColumnMapIn[] | undefined {
  if (!s.columns.length) return undefined;
  return s.columns.map((c) => ({
    source: c.source,
    name: c.name !== c.source ? c.name : undefined,
    type: c.type === AUTO_TYPE ? undefined : c.type,
    include: c.include,
  }));
}

export function buildJobIn(s: WizardState, name?: string): JobIn {
  return { name: name || null, source: buildSource(s), target: buildTarget(s), columns: buildColumns(s), options: buildOptions(s) };
}
