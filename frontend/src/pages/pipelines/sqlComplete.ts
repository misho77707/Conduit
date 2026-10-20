// Suggestions for a combined source's SQL: the inputs (by alias), their columns (after "alias." or a table alias
// from FROM / JOIN), DuckDB functions and keywords. Same popup and insertion rules as pyComplete.ts.
import { rank, type Completion, type Item } from "./pyComplete";

export interface SqlTable { alias: string; columns: { name: string; type: string }[] }

const KEYWORDS = [
  "SELECT", "FROM", "WHERE", "JOIN", "LEFT JOIN", "INNER JOIN", "FULL OUTER JOIN", "CROSS JOIN", "ON", "USING", "AS",
  "AND", "OR", "NOT", "IN", "IS NULL", "IS NOT NULL", "BETWEEN", "LIKE", "ILIKE", "CASE", "WHEN", "THEN", "ELSE", "END",
  "GROUP BY", "GROUP BY ALL", "HAVING", "ORDER BY", "DISTINCT", "WITH", "UNION ALL", "EXCLUDE", "QUALIFY", "OVER",
  "PARTITION BY", "LIMIT",
];

const FUNCS: [string, string, string][] = [
  ["count", "count(*)", "Rows per group; count(x) counts non-NULL values, count(DISTINCT x) distinct ones."],
  ["sum", "sum(x)", "Total per group."],
  ["avg", "avg(x)", "Average per group."],
  ["min", "min(x)", "Smallest value per group."],
  ["max", "max(x)", "Largest value per group."],
  ["any_value", "any_value(x)", "Any one value of the group (for columns that are the same within it)."],
  ["coalesce", "coalesce(a, b, …)", "The first value that is not NULL: coalesce(u.name, 'unknown')."],
  ["cast", "CAST(x AS BIGINT)", "Convert a value: needed when a join key is text on one side and a number on the other."],
  ["try_cast", "TRY_CAST(x AS BIGINT)", "Like CAST, but NULL instead of an error for values that do not convert."],
  ["date_trunc", "date_trunc('day', ts)", "Round a timestamp down to 'hour', 'day', 'week', 'month' …"],
  ["strftime", "strftime(ts, '%Y-%m-%d')", "Format a date/time as text."],
  ["lower", "lower(s)", "Lowercase text."],
  ["upper", "upper(s)", "Uppercase text."],
  ["trim", "trim(s)", "Remove leading and trailing spaces."],
  ["concat", "concat(a, b, …)", "Join text; NULLs count as empty."],
  ["round", "round(x, 2)", "Round to n decimals."],
  ["row_number", "row_number() OVER (PARTITION BY k ORDER BY ts DESC)", "Number rows within each group (staged mode)."],
  ["now", "now()", "The current time."],
];

const RESERVED = new Set(["select", "from", "where", "group", "order", "by", "join", "on", "as", "and", "or", "not", "table",
  "limit", "case", "when", "then", "else", "end", "user", "default", "to", "union", "all", "distinct", "having", "in", "is"]);

/** A column name as SQL: bare when it is a plain identifier, else "double quoted". */
export function ident(name: string): string {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(name) && !RESERVED.has(name.toLowerCase()) ? name : `"${name.replace(/"/g, "\"\"")}"`;
}

const NOT_ALIAS = new Set(["on", "using", "where", "left", "right", "inner", "full", "outer", "cross", "join", "natural",
  "group", "order", "limit", "union", "having", "qualify", "window", "as", "and", "or", "positional", "asof", "semi", "anti"]);

/** Names usable before a "." : every input's alias, and the short names given to them in FROM / JOIN. */
function qualifiers(code: string, tables: SqlTable[]): Map<string, SqlTable> {
  const by = new Map(tables.map((t) => [t.alias.toLowerCase(), t]));
  const out = new Map(by);
  const re = /\b(?:from|join)\s+"?([A-Za-z_]\w*)"?(?:\s+(?:as\s+)?([A-Za-z_]\w*))?/gi;
  for (let m = re.exec(code); m; m = re.exec(code)) {
    const t = by.get(m[1].toLowerCase());
    if (t && m[2] && !NOT_ALIAS.has(m[2].toLowerCase())) out.set(m[2].toLowerCase(), t);
  }
  return out;
}

function inStringOrComment(line: string): boolean {
  let q = false;
  for (let i = 0; i < line.length; i++) {
    if (line[i] === "'") q = !q;
    else if (!q && line[i] === "-" && line[i + 1] === "-") return true;
  }
  return q;
}

export function sqlComplete(code: string, caret: number, tables: SqlTable[], force = false): Completion | null {
  const before = code.slice(0, caret);
  if (inStringOrComment(before.slice(before.lastIndexOf("\n") + 1))) return null;
  const word = /[A-Za-z_]\w*$/.exec(before)?.[0] || "";
  const dotIdx = caret - word.length - 1;

  if (dotIdx >= 0 && code[dotIdx] === ".") { // alias.col
    const q = /"?([A-Za-z_]\w*)"?$/.exec(code.slice(0, dotIdx))?.[1];
    const t = q ? qualifiers(code, tables).get(q.toLowerCase()) : undefined;
    if (!t) return null;
    const items = t.columns.map((c): Item => ({ label: c.name, kind: "column", sig: `${q}.${ident(c.name)}  ${c.type}`, doc: `A column of ${t.alias}.`, insert: ident(c.name) }));
    const ranked = rank(items, word);
    return ranked.length ? { items: ranked, from: caret - word.length, to: caret, prefix: word } : null;
  }

  if (!word || (!force && word.length < 2)) return null;
  const owners = new Map<string, { type: string; in: string[] }>();
  for (const t of tables) for (const c of t.columns) {
    const o = owners.get(c.name) || { type: c.type, in: [] };
    o.in.push(t.alias);
    owners.set(c.name, o);
  }
  const items: Item[] = [
    ...tables.map((t): Item => ({ label: t.alias, kind: "table", sig: `${t.alias}  · ${t.columns.length} columns`, doc: "An input of this combined source, used as a table." })),
    ...[...owners.entries()].map(([name, o]): Item => ({ label: name, kind: "column", sig: `${ident(name)}  ${o.type}`, doc: `A column of ${o.in.join(", ")}.${o.in.length > 1 ? " Qualify it (alias.column) when inputs share the name." : ""}`, insert: ident(name) })),
    ...FUNCS.map(([label, sig, doc]): Item => ({ label, kind: "function", sig, doc })),
    ...KEYWORDS.map((k): Item => ({ label: k, kind: "keyword", sig: k })),
  ];
  const ranked = rank(items, word).filter((i) => i.label !== word);
  return ranked.length ? { items: ranked, from: caret - word.length, to: caret, prefix: word } : null;
}
