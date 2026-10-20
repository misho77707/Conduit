// Autocomplete for the pipeline Python step: a curated pandas / numpy catalog plus a tiny type tracker
// (df is a DataFrame, df["x"] a Series, df["x"].str the string accessor, `out = df.copy()` a DataFrame ...).
// No language server: everything is worked out from the text before the caret.

export type Kind = "method" | "property" | "function" | "column" | "keyword" | "module" | "variable" | "constant" | "table";
type T = "df" | "series" | "str" | "dt" | "groupby" | "loc" | "pd" | "np" | "scalar" | "other";

export interface Item {
  label: string;
  kind: Kind;
  sig?: string; // shown in the detail pane, e.g. "fillna(value)"
  doc?: string;
  insert?: string; // text to insert (default: label, plus "()" for methods / functions)
  caretBack?: number; // move the caret back this many chars after inserting (inside the parentheses)
  returns?: T;
}

// `from` .. `to` is replaced by the chosen item. With `dotted`, the range starts at the "." so a column can
// become df["name"]; every other item then re-inserts the dot itself.
export interface Completion { items: Item[]; from: number; to: number; prefix: string; dotted?: boolean }

const m = (label: string, sig: string, doc: string, returns: T = "other"): Item =>
  ({ label, kind: "method", sig, doc, returns });
const p = (label: string, doc: string, returns: T = "other"): Item => ({ label, kind: "property", sig: label, doc, returns });
const f = (label: string, sig: string, doc: string, returns: T = "other"): Item =>
  ({ label, kind: "function", sig, doc, returns });

const DF: Item[] = [
  p("columns", "Column labels. list(df.columns) gives a plain list of names."),
  p("dtypes", "The type of every column.", "series"),
  p("shape", "(rows, columns)."),
  p("index", "Row labels."),
  p("empty", "True when the DataFrame has no rows."),
  p("loc", "Select by label / boolean mask: df.loc[df[\"amt\"] > 0, [\"id\", \"amt\"]].", "loc"),
  p("iloc", "Select by position: df.iloc[0:10].", "loc"),
  p("T", "Transposed DataFrame.", "df"),
  m("assign", "assign(**columns)", "New columns from values or functions: df.assign(total=df[\"qty\"] * df[\"price\"]).", "df"),
  m("rename", "rename(columns={\"old\": \"new\"})", "Rename columns (or index labels).", "df"),
  m("drop", "drop(columns=[\"a\", \"b\"])", "Remove columns (or rows by label).", "df"),
  m("copy", "copy()", "An independent copy, safe to modify.", "df"),
  m("query", "query(\"amount > 0 and status == 'paid'\")", "Keep rows matching an expression string.", "df"),
  m("sort_values", "sort_values(by, ascending=True)", "Sort rows by one or more columns (within this batch).", "df"),
  m("fillna", "fillna(value)", "Replace missing values; value can be a dict per column.", "df"),
  m("dropna", "dropna(subset=None, how=\"any\")", "Drop rows that have missing values.", "df"),
  m("drop_duplicates", "drop_duplicates(subset=None, keep=\"first\")", "Remove duplicate rows (within this batch only).", "df"),
  m("duplicated", "duplicated(subset=None, keep=\"first\")", "Boolean Series marking repeated rows.", "series"),
  m("astype", "astype({\"col\": \"int64\"})", "Change column types.", "df"),
  m("convert_dtypes", "convert_dtypes()", "Pick the best nullable types for every column.", "df"),
  m("replace", "replace(to_replace, value)", "Replace values everywhere (or per column with dicts).", "df"),
  m("where", "where(cond, other)", "Keep values where cond is True, use other elsewhere.", "df"),
  m("mask", "mask(cond, other)", "Opposite of where: replace values where cond is True.", "df"),
  m("isna", "isna()", "Boolean DataFrame: True where a value is missing.", "df"),
  m("notna", "notna()", "Boolean DataFrame: True where a value is present.", "df"),
  m("isin", "isin(values)", "Boolean DataFrame: whether each value is in values.", "df"),
  m("apply", "apply(func, axis=1)", "Run func on every row (axis=1) or column. Slow on big batches; prefer vectorised code.", "series"),
  m("map", "map(func)", "Apply func to every single value.", "df"),
  m("merge", "merge(right, on=\"key\", how=\"left\")", "SQL-style join with another DataFrame.", "df"),
  m("join", "join(other, on=None, how=\"left\")", "Join on the index (or a key column).", "df"),
  m("groupby", "groupby(\"col\")", "Group rows; follow with .agg / .sum / .transform. Groups only see this batch.", "groupby"),
  m("melt", "melt(id_vars, value_vars)", "Wide to long.", "df"),
  m("pivot_table", "pivot_table(index, columns, values, aggfunc=\"sum\")", "Long to wide with aggregation.", "df"),
  m("explode", "explode(\"col\")", "One row per element of a list-valued column.", "df"),
  m("filter", "filter(like=\"amt\")", "Select columns by name pattern (items= / like= / regex=).", "df"),
  m("select_dtypes", "select_dtypes(include=\"number\")", "Columns of the given kinds.", "df"),
  m("set_index", "set_index(\"col\")", "Use a column as the row index.", "df"),
  m("reset_index", "reset_index(drop=True)", "Turn the index back into a column (or drop it). Do this before returning.", "df"),
  m("head", "head(n=5)", "First n rows.", "df"),
  m("tail", "tail(n=5)", "Last n rows.", "df"),
  m("sample", "sample(n=None, frac=None)", "Random rows.", "df"),
  m("nlargest", "nlargest(n, columns)", "Rows with the largest values.", "df"),
  m("nsmallest", "nsmallest(n, columns)", "Rows with the smallest values.", "df"),
  m("round", "round(decimals=0)", "Round numeric columns.", "df"),
  m("clip", "clip(lower=None, upper=None)", "Limit values to a range.", "df"),
  m("abs", "abs()", "Absolute values.", "df"),
  m("eval", "eval(\"qty * price\")", "Evaluate a column expression string.", "series"),
  m("insert", "insert(loc, column, value)", "Insert a column at a position (in place, returns None).", "other"),
  m("pop", "pop(\"col\")", "Remove a column and return it.", "series"),
  m("pipe", "pipe(func)", "func(df), for chaining.", "df"),
  m("sum", "sum(numeric_only=True)", "Column totals.", "series"),
  m("mean", "mean(numeric_only=True)", "Column means.", "series"),
  m("count", "count()", "Non-missing values per column.", "series"),
  m("nunique", "nunique()", "Distinct values per column.", "series"),
  m("describe", "describe()", "Summary statistics.", "df"),
  m("value_counts", "value_counts(subset=None)", "How often each row combination occurs.", "series"),
  m("itertuples", "itertuples(index=False)", "Iterate rows as named tuples (faster than iterrows).", "other"),
  m("iterrows", "iterrows()", "Iterate (index, row Series) pairs. Slow.", "other"),
  m("to_dict", "to_dict(orient=\"records\")", "Rows as a list of dicts.", "other"),
  m("infer_objects", "infer_objects()", "Better types for object columns.", "df"),
];

const SERIES: Item[] = [
  p("str", "String methods: .str.lower(), .str.contains(...), .str.split(...).", "str"),
  p("dt", "Date/time parts: .dt.year, .dt.date, .dt.strftime(...).", "dt"),
  p("dtype", "The type of the values."),
  p("name", "The column name."),
  p("size", "Number of values."),
  p("values", "The values as a numpy array."),
  m("astype", "astype(\"int64\")", "Change the type: \"Int64\" (nullable int), \"float64\", \"string\", \"boolean\", \"datetime64[ns]\".", "series"),
  m("fillna", "fillna(value)", "Replace missing values.", "series"),
  m("isna", "isna()", "True where the value is missing.", "series"),
  m("notna", "notna()", "True where a value is present.", "series"),
  m("map", "map(dict_or_func)", "Map every value through a dict or function: s.map({\"Y\": True, \"N\": False}).", "series"),
  m("apply", "apply(func)", "Run a Python function on every value (slower than vectorised methods).", "series"),
  m("replace", "replace(to_replace, value)", "Replace specific values.", "series"),
  m("where", "where(cond, other)", "Keep values where cond is True, other elsewhere.", "series"),
  m("mask", "mask(cond, other)", "Replace values where cond is True.", "series"),
  m("between", "between(left, right)", "True where left <= value <= right.", "series"),
  m("isin", "isin([\"a\", \"b\"])", "True where the value is in the list.", "series"),
  m("round", "round(decimals=0)", "Round numbers.", "series"),
  m("abs", "abs()", "Absolute values.", "series"),
  m("clip", "clip(lower=None, upper=None)", "Limit values to a range.", "series"),
  m("shift", "shift(periods=1)", "Move values down (or up) by n rows.", "series"),
  m("diff", "diff()", "Difference to the previous row.", "series"),
  m("cumsum", "cumsum()", "Running total (restarts every batch).", "series"),
  m("rank", "rank()", "Rank of each value within the batch.", "series"),
  m("pct_change", "pct_change()", "Change relative to the previous row.", "series"),
  m("ffill", "ffill()", "Fill missing values with the previous one.", "series"),
  m("bfill", "bfill()", "Fill missing values with the next one.", "series"),
  m("interpolate", "interpolate()", "Fill gaps by interpolation.", "series"),
  m("dropna", "dropna()", "Drop missing values.", "series"),
  m("duplicated", "duplicated(keep=\"first\")", "True for repeated values.", "series"),
  m("unique", "unique()", "Distinct values (array).", "other"),
  m("nunique", "nunique()", "Number of distinct values.", "scalar"),
  m("value_counts", "value_counts()", "How often each value occurs.", "series"),
  m("sort_values", "sort_values(ascending=True)", "Sorted values.", "series"),
  m("sum", "sum()", "Total.", "scalar"),
  m("mean", "mean()", "Average.", "scalar"),
  m("median", "median()", "Median.", "scalar"),
  m("min", "min()", "Smallest value.", "scalar"),
  m("max", "max()", "Largest value.", "scalar"),
  m("std", "std()", "Standard deviation.", "scalar"),
  m("count", "count()", "Number of non-missing values.", "scalar"),
  m("idxmax", "idxmax()", "Index label of the largest value.", "scalar"),
  m("to_list", "to_list()", "Plain Python list.", "other"),
  m("eq", "eq(other)", "Element-wise ==.", "series"),
  m("ne", "ne(other)", "Element-wise !=.", "series"),
  m("gt", "gt(other)", "Element-wise >.", "series"),
  m("lt", "lt(other)", "Element-wise <.", "series"),
  m("combine_first", "combine_first(other)", "Fill missing values from another Series.", "series"),
];

const STR: Item[] = [
  m("lower", "lower()", "Lowercase.", "series"),
  m("upper", "upper()", "Uppercase.", "series"),
  m("title", "title()", "Title Case.", "series"),
  m("capitalize", "capitalize()", "First letter uppercase.", "series"),
  m("strip", "strip(to_strip=None)", "Trim whitespace (or the given characters) on both ends.", "series"),
  m("lstrip", "lstrip()", "Trim on the left.", "series"),
  m("rstrip", "rstrip()", "Trim on the right.", "series"),
  m("replace", "replace(pat, repl, regex=False)", "Replace text; set regex=True for a regular expression.", "series"),
  m("contains", "contains(pat, case=True, regex=True, na=False)", "True where the text contains pat.", "series"),
  m("startswith", "startswith(prefix)", "True where the text starts with prefix.", "series"),
  m("endswith", "endswith(suffix)", "True where the text ends with suffix.", "series"),
  m("match", "match(pattern)", "True where the regex matches at the start.", "series"),
  m("fullmatch", "fullmatch(pattern)", "True where the regex matches the whole text.", "series"),
  m("len", "len()", "Length of each string.", "series"),
  m("split", "split(pat=None, n=-1, expand=False)", "Split into lists, or into columns with expand=True.", "series"),
  m("get", "get(i)", "Element i of each list / character i of each string.", "series"),
  m("slice", "slice(start=None, stop=None)", "Substring by position.", "series"),
  m("extract", "extract(r\"(\\d+)\")", "Regex capture groups into columns.", "df"),
  m("findall", "findall(pattern)", "All regex matches per value.", "series"),
  m("count", "count(pattern)", "Number of regex matches.", "series"),
  m("find", "find(sub)", "Position of sub, -1 if missing.", "series"),
  m("zfill", "zfill(width)", "Pad with leading zeros.", "series"),
  m("pad", "pad(width, side=\"left\", fillchar=\" \")", "Pad to a width.", "series"),
  m("cat", "cat(others=None, sep=\"\")", "Concatenate strings.", "series"),
  m("removeprefix", "removeprefix(prefix)", "Drop a leading prefix.", "series"),
  m("removesuffix", "removesuffix(suffix)", "Drop a trailing suffix.", "series"),
  m("isnumeric", "isnumeric()", "True where all characters are numeric.", "series"),
  m("isdigit", "isdigit()", "True where all characters are digits.", "series"),
  m("normalize", "normalize(\"NFKC\")", "Unicode normalisation.", "series"),
];

const DT: Item[] = [
  p("year", "Year.", "series"), p("month", "Month 1-12.", "series"), p("day", "Day of month.", "series"),
  p("hour", "Hour.", "series"), p("minute", "Minute.", "series"), p("second", "Second.", "series"),
  p("date", "Date part (Python date objects).", "series"), p("time", "Time part.", "series"),
  p("dayofweek", "Monday=0 … Sunday=6.", "series"), p("dayofyear", "Day of the year.", "series"),
  p("quarter", "Quarter 1-4.", "series"), p("days_in_month", "Days in that month.", "series"),
  p("is_month_end", "True on the last day of a month.", "series"),
  m("strftime", "strftime(\"%Y-%m-%d\")", "Format as text.", "series"),
  m("day_name", "day_name()", "\"Monday\", \"Tuesday\" …", "series"),
  m("month_name", "month_name()", "\"January\" …", "series"),
  m("floor", "floor(\"h\")", "Round down to a unit: \"D\", \"h\", \"15min\".", "series"),
  m("ceil", "ceil(\"h\")", "Round up to a unit.", "series"),
  m("round", "round(\"h\")", "Round to a unit.", "series"),
  m("normalize", "normalize()", "Set the time to midnight.", "series"),
  m("tz_localize", "tz_localize(\"UTC\")", "Attach a time zone to naive times.", "series"),
  m("tz_convert", "tz_convert(\"Asia/Tbilisi\")", "Convert to another time zone.", "series"),
  m("to_period", "to_period(\"M\")", "Calendar period (month, quarter …).", "series"),
  m("total_seconds", "total_seconds()", "For durations: length in seconds.", "series"),
];

const PD: Item[] = [
  f("to_datetime", "pd.to_datetime(arg, format=None, errors=\"coerce\", utc=False)", "Parse text/numbers into timestamps; errors=\"coerce\" turns bad values into NaT.", "series"),
  f("to_numeric", "pd.to_numeric(arg, errors=\"coerce\")", "Parse text into numbers; bad values become NaN.", "series"),
  f("to_timedelta", "pd.to_timedelta(arg, unit=None)", "Parse durations.", "series"),
  f("concat", "pd.concat(objs, ignore_index=True)", "Stack DataFrames (rows) or join them side by side (axis=1).", "df"),
  f("merge", "pd.merge(left, right, on=\"key\", how=\"left\")", "SQL-style join.", "df"),
  f("DataFrame", "pd.DataFrame(data)", "Build a DataFrame from dicts / lists.", "df"),
  f("Series", "pd.Series(data)", "Build a Series.", "series"),
  f("isna", "pd.isna(obj)", "True where missing (works on scalars too).", "other"),
  f("notna", "pd.notna(obj)", "True where present.", "other"),
  f("cut", "pd.cut(x, bins, labels=None)", "Bucket numbers into ranges.", "series"),
  f("qcut", "pd.qcut(x, q)", "Bucket by quantiles (within the batch).", "series"),
  f("get_dummies", "pd.get_dummies(df, columns=[...])", "One-hot encode columns.", "df"),
  f("json_normalize", "pd.json_normalize(records)", "Flatten nested dicts / JSON into columns.", "df"),
  f("Timestamp", "pd.Timestamp(\"2024-01-01\")", "One point in time.", "scalar"),
  f("Timedelta", "pd.Timedelta(days=1)", "A duration.", "scalar"),
  f("date_range", "pd.date_range(start, end, freq=\"D\")", "Evenly spaced timestamps.", "other"),
  { label: "NA", kind: "constant", sig: "pd.NA", doc: "Missing value marker for nullable types." },
  { label: "NaT", kind: "constant", sig: "pd.NaT", doc: "Missing timestamp." },
];

const NP: Item[] = [
  f("where", "np.where(cond, x, y)", "x where cond is True, else y: np.where(df[\"amt\"] > 0, \"in\", \"out\").", "other"),
  f("select", "np.select(condlist, choicelist, default=None)", "Like a CASE WHEN with several branches.", "other"),
  { label: "nan", kind: "constant", sig: "np.nan", doc: "Missing float value." },
  f("isnan", "np.isnan(x)", "True where NaN.", "other"),
  f("round", "np.round(x, decimals=0)", "Round.", "other"),
  f("floor", "np.floor(x)", "Round down.", "other"),
  f("ceil", "np.ceil(x)", "Round up.", "other"),
  f("abs", "np.abs(x)", "Absolute value.", "other"),
  f("sqrt", "np.sqrt(x)", "Square root.", "other"),
  f("log", "np.log(x)", "Natural log.", "other"),
  f("log10", "np.log10(x)", "Base-10 log.", "other"),
  f("exp", "np.exp(x)", "e to the power x.", "other"),
  f("clip", "np.clip(x, lo, hi)", "Limit to a range.", "other"),
  f("maximum", "np.maximum(a, b)", "Element-wise max.", "other"),
  f("minimum", "np.minimum(a, b)", "Element-wise min.", "other"),
  f("sign", "np.sign(x)", "-1, 0 or 1.", "other"),
  f("arange", "np.arange(n)", "0 … n-1.", "other"),
  f("array", "np.array(data)", "Build an array.", "other"),
];

const GROUPBY: Item[] = [
  m("agg", "agg(total=(\"amt\", \"sum\"), n=(\"id\", \"count\"))", "Named aggregations (per batch!).", "df"),
  m("sum", "sum()", "Sum per group.", "df"), m("mean", "mean()", "Mean per group.", "df"),
  m("count", "count()", "Count per group.", "df"), m("size", "size()", "Rows per group.", "series"),
  m("min", "min()", "Min per group.", "df"), m("max", "max()", "Max per group.", "df"),
  m("first", "first()", "First row per group.", "df"), m("last", "last()", "Last row per group.", "df"),
  m("transform", "transform(\"sum\")", "Group result broadcast back to every row (same length as df).", "series"),
  m("cumcount", "cumcount()", "Row number within each group.", "series"),
  m("ngroup", "ngroup()", "Group number for each row.", "series"),
];

const KEYWORDS = ["def", "return", "if", "elif", "else", "for", "in", "while", "import", "from", "as", "lambda", "None",
  "True", "False", "and", "or", "not", "is", "try", "except", "finally", "with", "pass", "break", "continue", "raise"];
const BUILTINS: [string, string][] = [
  ["len", "len(x)"], ["str", "str(x)"], ["int", "int(x)"], ["float", "float(x)"], ["bool", "bool(x)"], ["list", "list(x)"],
  ["dict", "dict(...)"], ["set", "set(x)"], ["tuple", "tuple(x)"], ["range", "range(n)"], ["enumerate", "enumerate(x)"],
  ["zip", "zip(a, b)"], ["sorted", "sorted(x)"], ["min", "min(...)"], ["max", "max(...)"], ["sum", "sum(x)"],
  ["abs", "abs(x)"], ["round", "round(x, n)"], ["isinstance", "isinstance(x, type)"], ["any", "any(x)"], ["all", "all(x)"],
  ["print", "print(...)  # output is not shown anywhere"],
];

const MEMBERS: Partial<Record<T, Item[]>> = { df: DF, series: SERIES, str: STR, dt: DT, pd: PD, np: NP, groupby: GROUPBY };

/** Plain-language list of what's covered, for the guide. */
export const CATALOG = { DataFrame: DF, Series: SERIES, ".str": STR, ".dt": DT, "pd.": PD, "np.": NP, groupby: GROUPBY };

// ---- tiny type tracker --------------------------------------------------------------

function matchingOpen(s: string, closeIdx: number): number {
  const close = s[closeIdx], open = close === "]" ? "[" : "(";
  let depth = 0;
  for (let i = closeIdx; i >= 0; i--) {
    if (s[i] === close) depth++;
    else if (s[i] === open && --depth === 0) return i;
  }
  return -1;
}

function typeOf(expr: string, vars: Map<string, T>): T {
  expr = expr.trim();
  if (!expr) return "other";
  if (/^[A-Za-z_]\w*$/.test(expr)) return vars.get(expr) || "other";
  if (expr.endsWith("]")) {
    const o = matchingOpen(expr, expr.length - 1);
    if (o <= 0) return "other";
    const base = expr.slice(0, o), inner = expr.slice(o + 1, -1).trim();
    if (/\.(loc|iloc)$/.test(base)) {
      const bt = typeOf(base.replace(/\.(loc|iloc)$/, ""), vars);
      return bt === "df" ? (/,\s*["'][^"']*["']\s*$/.test(inner) ? "series" : "df") : bt;
    }
    const bt = typeOf(base, vars);
    if (bt === "df") return /^["'][^"']*["']$/.test(inner) ? "series" : "df";
    return bt === "series" ? "series" : "other";
  }
  if (expr.endsWith(")")) {
    const o = matchingOpen(expr, expr.length - 1);
    if (o <= 0) return "other";
    const callee = expr.slice(0, o);
    const mm = /^(.*)\.([A-Za-z_]\w*)$/.exec(callee);
    if (!mm) return "other";
    const rt = typeOf(mm[1], vars);
    return MEMBERS[rt]?.find((i) => i.label === mm[2])?.returns || "other";
  }
  const mm = /^(.*)\.([A-Za-z_]\w*)$/.exec(expr);
  if (mm) {
    const rt = typeOf(mm[1], vars);
    if (rt === "df" && !MEMBERS.df!.some((i) => i.label === mm[2])) return "series"; // df.amount column access
    return MEMBERS[rt]?.find((i) => i.label === mm[2])?.returns || "other";
  }
  return "other";
}

function knownVars(code: string): Map<string, T> {
  const vars = new Map<string, T>([["df", "df"], ["pd", "pd"], ["np", "np"]]);
  for (const line of code.split("\n")) {
    const a = /^\s*([A-Za-z_]\w*)\s*=\s*(?!=)(.+?)\s*(#.*)?$/.exec(line);
    if (a) vars.set(a[1], typeOf(a[2], vars));
    const imp = /^\s*import\s+(pandas|numpy)\s+as\s+(\w+)/.exec(line);
    if (imp) vars.set(imp[2], imp[1] === "pandas" ? "pd" : "np");
  }
  return vars;
}

/** Walk back from the dot over identifiers, dots and balanced []/() to find the receiver expression. */
function receiverBefore(s: string, dotIdx: number): string {
  let i = dotIdx - 1;
  while (i >= 0) {
    const ch = s[i];
    if (ch === "]" || ch === ")") {
      const o = matchingOpen(s, i);
      if (o < 0) break;
      i = o - 1;
    } else if (/[\w.]/.test(ch)) i--;
    else break;
  }
  return s.slice(i + 1, dotIdx);
}

function inStringOrComment(line: string): boolean {
  let q: string | null = null;
  for (const ch of line) {
    if (q) { if (ch === q) q = null; }
    else if (ch === "'" || ch === "\"") q = ch;
    else if (ch === "#") return true;
  }
  return q !== null;
}

export function rank(items: Item[], prefix: string): Item[] {
  const p = prefix.toLowerCase();
  const starts = items.filter((i) => i.label.toLowerCase().startsWith(p));
  const contains = p ? items.filter((i) => !i.label.toLowerCase().startsWith(p) && i.label.toLowerCase().includes(p)) : [];
  const seen = new Set<string>();
  return [...starts, ...contains].filter((i) => (seen.has(i.kind + i.label) ? false : (seen.add(i.kind + i.label), true))).slice(0, 60);
}

/** Completions at `caret`, or null when nothing fits. `force` (Ctrl+Space) also completes bare words. */
export function complete(code: string, caret: number, columns: string[], force = false): Completion | null {
  const before = code.slice(0, caret);
  const lineStart = before.lastIndexOf("\n") + 1;
  const line = before.slice(lineStart);
  const vars = knownVars(code.slice(0, lineStart));

  // df["… : column names
  const col = /([\w.\]\)]+)\[\s*(["'])([^"'\]]*)$/.exec(line);
  if (col) {
    const t = typeOf(receiverBefore(before, caret - col[0].length + col[1].length), vars);
    if (t === "df" || t === "loc") {
      const q = col[2], prefix = col[3], after = code.slice(caret);
      const closing = after.startsWith(q + "]") ? 2 : after.startsWith(q) ? 1 : 0;
      const items = rank(columns.map((c) => ({ label: c, kind: "column" as Kind, sig: `column "${c}"`, doc: "A column of the data this step receives.", insert: `${c}${q}]` })), prefix);
      return items.length ? { items, from: caret - prefix.length, to: caret + closing, prefix } : null;
    }
  }
  if (inStringOrComment(line)) return null;

  const word = /[A-Za-z_]\w*$/.exec(line)?.[0] || "";
  const dotIdx = caret - word.length - 1;
  if (dotIdx >= 0 && code[dotIdx] === ".") {
    const recv = receiverBefore(code, dotIdx);
    const t = typeOf(recv, vars);
    let items: Item[] = MEMBERS[t] ? [...MEMBERS[t]!] : [];
    if (t === "other" && recv) items = [...DF, ...SERIES]; // unknown receiver: offer both rather than nothing
    if (t === "df") {
      // columns as df["name"] (works for any name, unlike attribute access)
      const cols = columns.map((c): Item => ({ label: c, kind: "column", sig: `df["${c}"]`, doc: "Column of the step's input. Inserted as [\"name\"].", insert: `["${c}"]` }));
      const ranked = rank([...cols, ...items], word);
      return ranked.length ? { items: ranked, from: dotIdx, to: caret, prefix: word, dotted: true } : null;
    }
    const ranked = rank(items, word);
    return ranked.length ? { items: ranked, from: caret - word.length, to: caret, prefix: word } : null;
  }

  if (!word || (!force && word.length < 2)) return null;
  const names: Item[] = [
    ...[...vars.entries()].map(([n, t]): Item => ({ label: n, kind: t === "pd" || t === "np" ? "module" : "variable", sig: n, doc: t === "df" ? "DataFrame" : t === "series" ? "Series" : t === "pd" ? "pandas (pre-imported)" : t === "np" ? "numpy (pre-imported)" : "variable" })),
    ...KEYWORDS.map((k): Item => ({ label: k, kind: "keyword", sig: k })),
    ...BUILTINS.map(([k, sig]): Item => ({ label: k, kind: "function", sig, doc: "Python built-in." })),
  ];
  const ranked = rank(names, word).filter((i) => i.label !== word);
  return ranked.length ? { items: ranked, from: caret - word.length, to: caret, prefix: word } : null;
}

/** The text an item inserts, and how far back from its end the caret goes. */
export function insertion(item: Item, c: Completion): { text: string; back: number } {
  if (item.insert != null) return { text: item.insert, back: 0 }; // df.am| -> df["amount"] (range includes the dot)
  const dot = c.dotted ? "." : "";
  if (item.kind === "method" || item.kind === "function") return { text: `${dot}${item.label}()`, back: 1 };
  return { text: `${dot}${item.label}`, back: 0 };
}
