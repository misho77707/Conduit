// Helpers for a pipeline's combined source (several inputs joined by one DuckDB SELECT): defaults, labels and the
// checks the builder runs before saving. Mirrors backend/app/engine/spec.py CombinedIn.
import { databasesOnly, type CombinedIn, type ConnectionOut, type SourceIn } from "../../lib/api";
import { remoteLabel } from "../remote/RemotePicker";

export const MAX_INPUTS = 8;
export const ALIAS_RE = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/;

const SHORT: Record<string, string> = {
  postgres: "Postgres", clickhouse: "ClickHouse", mysql: "MySQL", sqlite: "SQLite", duckdb: "DuckDB", mongodb: "MongoDB",
  s3: "S3", sftp: "SFTP", http: "URL",
};

/** Where an input's rows live, in a word: "ClickHouse", "file", "S3". */
export function inputPlace(s: SourceIn, conns: ConnectionOut[]): string {
  if (s.file) return "file";
  if (s.remote) {
    const c = conns.find((x) => x.id === s.remote!.connection_id);
    return c ? SHORT[c.type] || c.type : "URL";
  }
  const c = conns.find((x) => x.id === s.connection_id);
  return c ? SHORT[c.type] || c.type : "?";
}

/** What an input reads, briefly: a table name, a file name, "query". */
export function inputWhat(s: SourceIn, conns: ConnectionOut[]): string {
  if (s.remote) return (remoteLabel(s.remote, conns).split("/").pop() || s.remote.path) || "";
  if (s.file) return s.file.path.split("/").pop() || "";
  if (s.query?.trim()) return "query";
  return s.table || "";
}

/** "events (ClickHouse) + users (Postgres)" */
export function combinedLabel(c: CombinedIn, conns: ConnectionOut[]): string {
  return c.inputs.map((i) => `${i.alias} (${inputPlace(i.source, conns)})`).join(" + ");
}

/** An alias from a table / file name: "public.orders" → orders, "exports/day_1.csv.gz" → day_1. */
export function aliasFrom(name: string, taken: string[]): string {
  let base = name.split(/[\\/]/).pop() || "";
  while (/\.(csv|tsv|txt|json|ndjson|jsonl|parquet|xlsx|gz|zip)$/i.test(base)) base = base.replace(/\.[^.]+$/, "");
  base = (base.split(".").pop() || "").toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^(\d)/, "_$1").slice(0, 40) || "input";
  const used = new Set(taken.map((t) => t.toLowerCase()));
  let alias = base;
  for (let n = 2; used.has(alias.toLowerCase()); n++) alias = `${base}_${n}`;
  return alias;
}

export function emptyInputSource(conns: ConnectionOut[]): SourceIn {
  return { connection_id: databasesOnly(conns)[0]?.id || null, table: "", where: "" };
}

/** A two-input combined source; the current (single) source becomes the first input. */
export function newCombined(current: SourceIn, conns: ConnectionOut[]): CombinedIn {
  const had = !!(current.table?.trim() || current.query?.trim() || current.file || current.remote);
  const first: SourceIn = had
    ? { connection_id: current.connection_id ?? null, table: current.table ?? null, query: current.query ?? null, where: current.where || "", file: current.file ?? null, remote: current.remote ?? null }
    : emptyInputSource(conns);
  const a = had ? aliasFrom(inputWhat(first, conns) === "query" ? "input1" : inputWhat(first, conns), []) : "input1";
  const b = a === "input2" ? "input3" : "input2";
  return {
    inputs: [{ alias: a, source: first, columns: null }, { alias: b, source: emptyInputSource(conns), columns: null }],
    sql: `SELECT *\nFROM ${a}\n-- LEFT JOIN ${b} ON ${b}.id = ${a}.${b}_id\n`,
    mode: "staged", driver: null, watermark: null,
  };
}

/** Why the combined source can't be saved yet (an input without a table / file, a bad alias ...), or null. */
export function combinedProblem(c: CombinedIn): string | null {
  const seen = new Set<string>();
  for (const [n, i] of c.inputs.entries()) {
    const name = i.alias.trim() || `#${n + 1}`;
    if (!ALIAS_RE.test(i.alias)) return `Input ${name}: the alias must be a plain name (letters, digits, _; not starting with a digit)`;
    if (seen.has(i.alias.toLowerCase())) return `Two inputs are called ${i.alias}: each needs its own alias`;
    seen.add(i.alias.toLowerCase());
    const s = i.source;
    if (s.remote && !s.remote.path.trim()) return `Input ${name}: choose a remote file`;
    if (s.file && !s.file.path.trim()) return `Input ${name}: enter a file path`;
    if (!s.remote && !s.file && !(s.connection_id && (s.table?.trim() || s.query?.trim()))) return `Input ${name}: choose a connection and a table or query`;
  }
  if (!c.sql.trim()) return "Write the SQL that combines the inputs";
  if (c.watermark && !c.watermark.column) return `Choose the watermark column of ${c.driver || "the driver"}`;
  return null;
}

/** The pipeline's source is ready to save (a combined source is checked input by input). */
export function sourceProblem(s: SourceIn): string | null {
  return s.combined ? combinedProblem(s.combined) : null;
}

export const isTimeType = (t: string | undefined) => !!t && /date|time/i.test(t);
