import type { StepIn, StepKind } from "../../lib/api";

export const STEP_META: Record<StepKind, { icon: string; label: string; group: "visual" | "code" }> = {
  rename: { icon: "edit", label: "Rename columns", group: "visual" },
  cast: { icon: "swap_horiz", label: "Cast types", group: "visual" },
  filter: { icon: "filter_alt", label: "Filter rows", group: "visual" },
  derive: { icon: "functions", label: "Derive column", group: "visual" },
  split: { icon: "call_split", label: "Split column", group: "visual" },
  sql: { icon: "code", label: "SQL transform", group: "code" },
  python: { icon: "terminal", label: "Python transform", group: "code" },
};

let seq = 0;
export function newStepId(kind: StepKind): string {
  seq += 1;
  return `${kind}_${Date.now().toString(36)}${seq}`;
}

export type PyEngine = "pandas" | "polars";

/** A Python step's untouched starter code, per engine (switching engine swaps it while it is untouched). */
export const PY_TEMPLATES: Record<PyEngine, string> = {
  pandas: "def transform(df):\n    # df is a pandas DataFrame\n    return df",
  polars: "def transform(df):\n    # df is a Polars DataFrame: return df.with_columns(...), df.filter(...) ...\n    return df",
};

/** A Python step's engine: steps saved before Polars existed have none, and are pandas. */
export const pyEngine = (config: Record<string, unknown>): PyEngine => (config.engine === "polars" ? "polars" : "pandas");

export function defaultConfig(kind: StepKind): Record<string, unknown> {
  switch (kind) {
    case "rename": return { mapping: {} };
    case "cast": return { mapping: {} };
    case "filter": return { expr: "" };
    case "derive": return { column: "", expr: "" };
    case "split": return { column: "", drop_source: false, into: [] };
    case "sql": return { sql: "SELECT *\nFROM {{ input }}\n" };
    case "python": return { engine: "polars", code: PY_TEMPLATES.polars };  // new steps: Polars
  }
}

export function newStep(kind: StepKind): StepIn {
  return { id: newStepId(kind), kind, config: defaultConfig(kind) };
}

export function stepSummary(step: StepIn): string {
  const c = step.config as Record<string, any>;
  switch (step.kind) {
    case "rename": { const n = Object.keys(c.mapping || {}).length; return n ? `${n} column${n === 1 ? "" : "s"} renamed` : "no columns yet"; }
    case "cast": { const n = Object.keys(c.mapping || {}).length; return n ? `${n} column${n === 1 ? "" : "s"} cast` : "no columns yet"; }
    case "filter": return c.expr || "no condition yet";
    case "derive": return c.column ? `${c.column} = ${c.expr || "…"}` : "no column yet";
    case "split": return c.column ? `${c.column} → ${(c.into || []).length} column(s)` : "no column yet";
    case "sql": return (c.sql || "").split("\n")[0] || "empty";
    case "python": return `transform(df) · ${pyEngine(c)}`;
  }
}
