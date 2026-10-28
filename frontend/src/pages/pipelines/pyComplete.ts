// Autocomplete for the pipeline Python step: a curated pandas / numpy catalog plus a tiny type tracker
// (df is a DataFrame, df["x"] a Series, df["x"].str the string accessor, `out = df.copy()` a DataFrame ...).
// No language server: everything is worked out from the text before the caret.

export type Kind = "method" | "property" | "function" | "column" | "keyword" | "module" | "variable" | "constant" | "table";
type T = "df" | "series" | "str" | "dt" | "groupby" | "loc" | "pd" | "np" | "scalar" | "other"
  // Polars: the module, DataFrame, LazyFrame, expressions and their namespaces, group_by, Series, when/then chains
  | "pl" | "pldf" | "lazy" | "expr" | "plstr" | "pldt" | "pllist" | "plgroupby" | "plseries" | "when" | "then";

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

// ---- Polars (a step with engine "polars": df is a polars DataFrame, pl and np are pre-imported) --------------
// Every name here exists in Polars 2.x: backend/tests/test_steps_polars.py checks this file against the installed one.

const PL: Item[] = [
  f("col", "pl.col(\"name\")", "A column, as an expression: pl.col(\"amount\") * 2. Several names or a dtype select several.", "expr"),
  f("lit", "pl.lit(value)", "A constant as an expression: pl.lit(\"EUR\").alias(\"currency\").", "expr"),
  f("when", "pl.when(cond).then(x).otherwise(y)", "CASE WHEN: chain more .when(...).then(...) before .otherwise(...).", "when"),
  f("all", "pl.all()", "Every column, as one expression: df.select(pl.all().fill_null(0)).", "expr"),
  f("exclude", "pl.exclude(\"a\", \"b\")", "Every column but these.", "expr"),
  f("len", "pl.len()", "Number of rows (per group inside agg).", "expr"),
  f("element", "pl.element()", "The element inside .list.eval(...).", "expr"),
  f("concat_str", "pl.concat_str([pl.col(\"a\"), pl.col(\"b\")], separator=\" \")", "Join text columns into one.", "expr"),
  f("format", "pl.format(\"{}-{}\", \"a\", \"b\")", "Build text from a template and columns.", "expr"),
  f("coalesce", "pl.coalesce(\"a\", \"b\", pl.lit(0))", "The first non-null value of several columns.", "expr"),
  f("sum_horizontal", "pl.sum_horizontal(\"a\", \"b\")", "Row-wise sum across columns.", "expr"),
  f("max_horizontal", "pl.max_horizontal(\"a\", \"b\")", "Row-wise maximum.", "expr"),
  f("min_horizontal", "pl.min_horizontal(\"a\", \"b\")", "Row-wise minimum.", "expr"),
  f("mean_horizontal", "pl.mean_horizontal(\"a\", \"b\")", "Row-wise mean.", "expr"),
  f("any_horizontal", "pl.any_horizontal(cond1, cond2)", "Row-wise OR.", "expr"),
  f("all_horizontal", "pl.all_horizontal(cond1, cond2)", "Row-wise AND.", "expr"),
  f("struct", "pl.struct(\"a\", \"b\")", "Pack columns into one struct column.", "expr"),
  f("date", "pl.date(2024, 1, 31)", "A date (or one built from year / month / day columns).", "expr"),
  f("datetime", "pl.datetime(2024, 1, 31, 12)", "A date-time from parts.", "expr"),
  f("duration", "pl.duration(days=1)", "A duration, to add to dates: pl.col(\"d\") + pl.duration(days=1).", "expr"),
  f("int_range", "pl.int_range(pl.len())", "0, 1, 2 … as an expression (a row number within the batch).", "expr"),
  f("concat", "pl.concat([df1, df2], how=\"vertical\")", "Stack frames (or side by side with how=\"horizontal\").", "pldf"),
  f("DataFrame", "pl.DataFrame(data)", "Build a DataFrame from dicts / lists.", "pldf"),
  f("Series", "pl.Series(\"name\", values)", "Build a Series.", "plseries"),
  f("from_dicts", "pl.from_dicts(rows)", "A DataFrame from a list of dicts.", "pldf"),
  ...["Int8", "Int16", "Int32", "Int64", "UInt32", "UInt64", "Float32", "Float64", "Boolean", "String", "Date", "Time",
    "Categorical"].map((t): Item => ({ label: t, kind: "constant", sig: `pl.${t}`, doc: `Data type, for .cast(pl.${t}).` })),
  f("Decimal", "pl.Decimal(precision, scale)", "Exact decimal type: .cast(pl.Decimal(12, 2)).", "other"),
  f("Datetime", "pl.Datetime(\"us\", time_zone=None)", "Date-time type, optionally with a time zone.", "other"),
  f("Duration", "pl.Duration(\"us\")", "Duration type.", "other"),
  f("List", "pl.List(pl.String)", "List type.", "other"),
];

const PLDF: Item[] = [
  p("columns", "Column names (a list)."),
  p("schema", "Name -> data type of every column."),
  p("dtypes", "The data type of every column."),
  p("shape", "(rows, columns)."),
  p("height", "Number of rows."),
  p("width", "Number of columns."),
  m("select", "select(\"a\", pl.col(\"b\") * 2)", "Keep / compute exactly these columns.", "pldf"),
  m("with_columns", "with_columns(new=pl.col(\"a\") + 1)", "Add or replace columns, keep the rest. The expressions run in parallel.", "pldf"),
  m("filter", "filter(pl.col(\"amount\") > 0)", "Keep rows where the condition is true (several conditions are ANDed).", "pldf"),
  m("rename", "rename({\"old\": \"new\"})", "Rename columns.", "pldf"),
  m("drop", "drop(\"a\", \"b\")", "Remove columns.", "pldf"),
  m("cast", "cast({\"a\": pl.Int64})", "Change column types.", "pldf"),
  m("sort", "sort(\"col\", descending=False)", "Sort rows (within this batch).", "pldf"),
  m("unique", "unique(subset=None, keep=\"first\")", "Remove duplicate rows (within this batch only).", "pldf"),
  m("drop_nulls", "drop_nulls(subset=None)", "Drop rows with nulls.", "pldf"),
  m("fill_null", "fill_null(value)", "Replace nulls (or strategy=\"forward\").", "pldf"),
  m("fill_nan", "fill_nan(value)", "Replace float NaN values.", "pldf"),
  m("with_row_index", "with_row_index(\"row\")", "Add a 0-based row number column (restarts every batch).", "pldf"),
  m("join", "join(other, on=\"key\", how=\"left\")", "SQL-style join with another DataFrame.", "pldf"),
  m("group_by", "group_by(\"col\")", "Group rows; follow with .agg(...). Groups only see this batch.", "plgroupby"),
  m("explode", "explode(\"col\")", "One row per element of a list column.", "pldf"),
  m("unnest", "unnest(\"col\")", "Struct column -> one column per field.", "pldf"),
  m("unpivot", "unpivot(index=\"id\", on=[\"a\", \"b\"])", "Wide to long (melt).", "pldf"),
  m("pivot", "pivot(on=\"key\", index=\"id\", values=\"v\")", "Long to wide.", "pldf"),
  m("head", "head(n=5)", "First n rows.", "pldf"),
  m("tail", "tail(n=5)", "Last n rows.", "pldf"),
  m("slice", "slice(offset, length)", "Rows by position.", "pldf"),
  m("sample", "sample(n=None, fraction=None)", "Random rows.", "pldf"),
  m("top_k", "top_k(k, by=\"col\")", "The k rows with the largest values.", "pldf"),
  m("bottom_k", "bottom_k(k, by=\"col\")", "The k rows with the smallest values.", "pldf"),
  m("lazy", "lazy()", "A LazyFrame: chain steps and Polars optimises them; returning it is fine (Conduit collects it).", "lazy"),
  m("clone", "clone()", "A copy (cheap: the data is shared).", "pldf"),
  m("pipe", "pipe(func)", "func(df), for chaining.", "pldf"),
  m("get_column", "get_column(\"name\")", "One column as a Series.", "plseries"),
  m("to_series", "to_series(index=0)", "A column by position as a Series.", "plseries"),
  m("hstack", "hstack([series])", "Add Series as columns.", "pldf"),
  m("vstack", "vstack(other)", "Append the rows of another frame.", "pldf"),
  m("null_count", "null_count()", "Nulls per column.", "pldf"),
  m("describe", "describe()", "Summary statistics.", "pldf"),
  m("is_empty", "is_empty()", "True when there are no rows.", "scalar"),
  m("to_dicts", "to_dicts()", "Rows as a list of dicts.", "other"),
  m("rows", "rows()", "Rows as a list of tuples.", "other"),
  m("iter_rows", "iter_rows(named=True)", "Iterate rows (slow on big batches: prefer expressions).", "other"),
  m("to_pandas", "to_pandas()", "Convert to pandas (transform must still return a polars frame).", "other"),
];

const LAZY: Item[] = [
  m("select", "select(...)", "Keep / compute exactly these columns.", "lazy"),
  m("with_columns", "with_columns(...)", "Add or replace columns.", "lazy"),
  m("filter", "filter(cond)", "Keep rows where cond is true.", "lazy"),
  m("rename", "rename({\"old\": \"new\"})", "Rename columns.", "lazy"),
  m("drop", "drop(\"a\")", "Remove columns.", "lazy"),
  m("cast", "cast({\"a\": pl.Int64})", "Change column types.", "lazy"),
  m("sort", "sort(\"col\")", "Sort rows (within this batch).", "lazy"),
  m("unique", "unique(subset=None)", "Remove duplicate rows (within this batch).", "lazy"),
  m("drop_nulls", "drop_nulls()", "Drop rows with nulls.", "lazy"),
  m("fill_null", "fill_null(value)", "Replace nulls.", "lazy"),
  m("join", "join(other, on=\"key\", how=\"left\")", "SQL-style join (other is a LazyFrame).", "lazy"),
  m("group_by", "group_by(\"col\")", "Group rows; follow with .agg(...).", "plgroupby"),
  m("explode", "explode(\"col\")", "One row per list element.", "lazy"),
  m("unnest", "unnest(\"col\")", "Struct -> columns.", "lazy"),
  m("with_row_index", "with_row_index(\"row\")", "Add a row number.", "lazy"),
  m("head", "head(n=5)", "First n rows.", "lazy"),
  m("limit", "limit(n=5)", "First n rows.", "lazy"),
  m("pipe", "pipe(func)", "func(lf), for chaining.", "lazy"),
  m("collect", "collect()", "Run the plan: a DataFrame. Optional in transform: Conduit collects a returned LazyFrame.", "pldf"),
  m("collect_schema", "collect_schema()", "The result's columns and types, without running the plan.", "other"),
];

const EXPR: Item[] = [
  p("str", "Text functions: .str.to_uppercase(), .str.contains(...), .str.to_date(...).", "plstr"),
  p("dt", "Date / time parts: .dt.year(), .dt.truncate(\"1mo\"), .dt.to_string(...).", "pldt"),
  p("list", "List functions: .list.len(), .list.get(0), .list.join(\",\").", "pllist"),
  m("alias", "alias(\"name\")", "Name the resulting column.", "expr"),
  m("cast", "cast(pl.Int64, strict=True)", "Change the type; strict=False turns failures into null.", "expr"),
  m("is_null", "is_null()", "True where the value is null.", "expr"),
  m("is_not_null", "is_not_null()", "True where a value is present.", "expr"),
  m("fill_null", "fill_null(value)", "Replace nulls (or strategy=\"forward\").", "expr"),
  m("fill_nan", "fill_nan(value)", "Replace float NaN.", "expr"),
  m("is_in", "is_in([\"a\", \"b\"])", "True where the value is in the list.", "expr"),
  m("is_between", "is_between(lower, upper)", "True where lower <= value <= upper.", "expr"),
  m("replace", "replace({\"Y\": \"yes\"})", "Map some values, keep the others.", "expr"),
  m("replace_strict", "replace_strict({\"Y\": True, \"N\": False}, default=None)", "Map every value (unmapped: default).", "expr"),
  m("round", "round(decimals=0)", "Round numbers.", "expr"),
  m("abs", "abs()", "Absolute values.", "expr"),
  m("clip", "clip(lower, upper)", "Limit values to a range.", "expr"),
  m("floor", "floor()", "Round down.", "expr"),
  m("ceil", "ceil()", "Round up.", "expr"),
  m("sqrt", "sqrt()", "Square root.", "expr"),
  m("log", "log(base=e)", "Logarithm.", "expr"),
  m("exp", "exp()", "e to the power of the value.", "expr"),
  m("pow", "pow(exponent)", "Power.", "expr"),
  m("shift", "shift(n=1)", "Values moved down (or up) by n rows.", "expr"),
  m("diff", "diff()", "Difference to the previous row.", "expr"),
  m("cum_sum", "cum_sum()", "Running total (restarts every batch).", "expr"),
  m("rank", "rank()", "Rank within the batch.", "expr"),
  m("over", "over(\"group_col\")", "Run the expression per group and keep every row: pl.col(\"amt\").sum().over(\"acct\").", "expr"),
  m("sum", "sum()", "Total.", "expr"), m("mean", "mean()", "Average.", "expr"), m("median", "median()", "Median.", "expr"),
  m("min", "min()", "Smallest value.", "expr"), m("max", "max()", "Largest value.", "expr"), m("std", "std()", "Standard deviation.", "expr"),
  m("count", "count()", "Non-null values.", "expr"), m("len", "len()", "Values, nulls included.", "expr"),
  m("n_unique", "n_unique()", "Distinct values.", "expr"), m("null_count", "null_count()", "Nulls.", "expr"),
  m("first", "first()", "First value.", "expr"), m("last", "last()", "Last value.", "expr"),
  m("unique", "unique()", "Distinct values.", "expr"),
  m("sort", "sort(descending=False)", "Sorted values.", "expr"),
  m("is_duplicated", "is_duplicated()", "True for values that occur more than once.", "expr"),
  m("is_unique", "is_unique()", "True for values that occur once.", "expr"),
  m("forward_fill", "forward_fill()", "Fill nulls with the previous value.", "expr"),
  m("backward_fill", "backward_fill()", "Fill nulls with the next value.", "expr"),
  m("not_", "not_()", "Logical NOT (also ~expr).", "expr"),
  m("map_elements", "map_elements(func, return_dtype=pl.String)", "A Python function per value: slow, prefer expressions.", "expr"),
  m("map_batches", "map_batches(func)", "A function on the whole column (a Series).", "expr"),
  m("implode", "implode()", "All values into one list.", "expr"),
  m("explode", "explode()", "One value per list element.", "expr"),
  m("head", "head(n=10)", "First n values.", "expr"),
  m("tail", "tail(n=10)", "Last n values.", "expr"),
];

const PLSTR: Item[] = [
  m("to_uppercase", "to_uppercase()", "Uppercase.", "expr"),
  m("to_lowercase", "to_lowercase()", "Lowercase.", "expr"),
  m("to_titlecase", "to_titlecase()", "Title Case.", "expr"),
  m("strip_chars", "strip_chars(characters=None)", "Trim whitespace (or the given characters) on both ends.", "expr"),
  m("strip_chars_start", "strip_chars_start()", "Trim on the left.", "expr"),
  m("strip_chars_end", "strip_chars_end()", "Trim on the right.", "expr"),
  m("strip_prefix", "strip_prefix(prefix)", "Drop a leading prefix.", "expr"),
  m("strip_suffix", "strip_suffix(suffix)", "Drop a trailing suffix.", "expr"),
  m("contains", "contains(pattern, literal=False)", "True where the text matches (a regex unless literal=True).", "expr"),
  m("starts_with", "starts_with(prefix)", "True where the text starts with prefix.", "expr"),
  m("ends_with", "ends_with(suffix)", "True where the text ends with suffix.", "expr"),
  m("replace", "replace(pattern, value, literal=False)", "Replace the first match.", "expr"),
  m("replace_all", "replace_all(pattern, value, literal=False)", "Replace every match.", "expr"),
  m("len_chars", "len_chars()", "Length in characters.", "expr"),
  m("len_bytes", "len_bytes()", "Length in bytes.", "expr"),
  m("slice", "slice(offset, length=None)", "Substring by position.", "expr"),
  m("head", "head(n)", "First n characters.", "expr"),
  m("tail", "tail(n)", "Last n characters.", "expr"),
  m("split", "split(\",\")", "Split into a list.", "expr"),
  m("split_exact", "split_exact(\"-\", 2)", "Split into a struct of n+1 fields (then df.unnest(...)).", "expr"),
  m("extract", "extract(r\"(\\d+)\", group_index=1)", "A regex capture group.", "expr"),
  m("extract_all", "extract_all(r\"\\d+\")", "Every regex match, as a list.", "expr"),
  m("count_matches", "count_matches(pattern)", "Number of regex matches.", "expr"),
  m("find", "find(pattern)", "Position of the first match (null if none).", "expr"),
  m("to_date", "to_date(\"%d.%m.%Y\", strict=False)", "Parse text into dates; strict=False turns bad values into null.", "expr"),
  m("to_datetime", "to_datetime(\"%Y-%m-%d %H:%M:%S\", strict=False)", "Parse text into date-times.", "expr"),
  m("strptime", "strptime(pl.Date, \"%d.%m.%Y\")", "Parse text into the given temporal type.", "expr"),
  m("to_integer", "to_integer(strict=False)", "Parse text into integers.", "expr"),
  m("to_decimal", "to_decimal()", "Parse text into decimals.", "expr"),
  m("zfill", "zfill(width)", "Pad with leading zeros.", "expr"),
  m("pad_start", "pad_start(width, fill_char=\" \")", "Pad on the left.", "expr"),
  m("pad_end", "pad_end(width, fill_char=\" \")", "Pad on the right.", "expr"),
  m("reverse", "reverse()", "Reverse the text.", "expr"),
  m("json_decode", "json_decode()", "Parse JSON text into structs / lists.", "expr"),
  m("json_path_match", "json_path_match(\"$.a.b\")", "One value out of JSON text.", "expr"),
  m("join", "join(\",\")", "Every value of the column joined into one text.", "expr"),
];

const PLDT: Item[] = [
  m("year", "year()", "Year.", "expr"), m("month", "month()", "Month 1-12.", "expr"), m("day", "day()", "Day of month.", "expr"),
  m("hour", "hour()", "Hour.", "expr"), m("minute", "minute()", "Minute.", "expr"), m("second", "second()", "Second.", "expr"),
  m("weekday", "weekday()", "Monday=1 … Sunday=7.", "expr"), m("ordinal_day", "ordinal_day()", "Day of the year.", "expr"),
  m("week", "week()", "ISO week number.", "expr"), m("quarter", "quarter()", "Quarter 1-4.", "expr"),
  m("date", "date()", "The date part.", "expr"), m("time", "time()", "The time part.", "expr"),
  m("to_string", "to_string(\"%Y-%m-%d\")", "Format as text.", "expr"),
  m("strftime", "strftime(\"%Y-%m-%d\")", "Format as text (same as to_string).", "expr"),
  m("truncate", "truncate(\"1mo\")", "Round down to a unit: \"1d\", \"1h\", \"15m\", \"1mo\".", "expr"),
  m("round", "round(\"1h\")", "Round to a unit.", "expr"),
  m("offset_by", "offset_by(\"1d\")", "Shift by a calendar duration: \"1mo\", \"-2d\".", "expr"),
  m("month_start", "month_start()", "First day of the month.", "expr"),
  m("month_end", "month_end()", "Last day of the month.", "expr"),
  m("epoch", "epoch(\"s\")", "Seconds (or \"ms\", \"us\", \"d\") since 1970.", "expr"),
  m("timestamp", "timestamp(\"ms\")", "Milliseconds (or \"us\", \"ns\") since 1970.", "expr"),
  m("convert_time_zone", "convert_time_zone(\"Asia/Tbilisi\")", "The same instant in another zone.", "expr"),
  m("replace_time_zone", "replace_time_zone(\"UTC\")", "Attach (or drop, with None) a zone without moving the clock.", "expr"),
  m("total_seconds", "total_seconds()", "For durations: length in seconds.", "expr"),
  m("total_days", "total_days()", "For durations: length in days.", "expr"),
];

const PLLIST: Item[] = [
  m("len", "len()", "Number of elements.", "expr"), m("get", "get(0, null_on_oob=True)", "Element at a position.", "expr"),
  m("first", "first()", "First element.", "expr"), m("last", "last()", "Last element.", "expr"),
  m("contains", "contains(item)", "True where the list holds item.", "expr"),
  m("join", "join(\",\")", "Elements joined into text.", "expr"),
  m("sum", "sum()", "Sum of the elements.", "expr"), m("mean", "mean()", "Mean.", "expr"),
  m("min", "min()", "Smallest element.", "expr"), m("max", "max()", "Largest element.", "expr"),
  m("sort", "sort()", "Sorted elements.", "expr"), m("unique", "unique()", "Distinct elements.", "expr"),
  m("n_unique", "n_unique()", "Number of distinct elements.", "expr"),
  m("slice", "slice(offset, length)", "Part of each list.", "expr"),
  m("head", "head(n)", "First n elements.", "expr"), m("tail", "tail(n)", "Last n elements.", "expr"),
  m("reverse", "reverse()", "Reversed.", "expr"), m("drop_nulls", "drop_nulls()", "Without nulls.", "expr"),
  m("eval", "eval(pl.element() * 2)", "An expression on every element (pl.element() is the element).", "expr"),
  m("explode", "explode()", "One row per element.", "expr"),
  m("to_struct", "to_struct()", "A struct with one field per position.", "expr"),
];

const PLGROUPBY: Item[] = [
  m("agg", "agg(total=pl.col(\"amt\").sum(), n=pl.len())", "Aggregate each group (per batch!).", "pldf"),
  m("len", "len()", "Rows per group.", "pldf"),
  m("sum", "sum()", "Sum per group.", "pldf"), m("mean", "mean()", "Mean per group.", "pldf"),
  m("min", "min()", "Min per group.", "pldf"), m("max", "max()", "Max per group.", "pldf"),
  m("first", "first()", "First row per group.", "pldf"), m("last", "last()", "Last row per group.", "pldf"),
  m("n_unique", "n_unique()", "Distinct values per group.", "pldf"),
  m("head", "head(n=5)", "First n rows of each group.", "pldf"),
];

const PLSERIES: Item[] = [
  p("str", "Text functions.", "plstr"), p("dt", "Date / time parts.", "pldt"), p("list", "List functions.", "pllist"),
  p("name", "The column name."), p("dtype", "The data type."),
  m("alias", "alias(\"name\")", "A renamed copy.", "plseries"),
  m("cast", "cast(pl.Int64)", "Change the type.", "plseries"),
  m("is_null", "is_null()", "True where null.", "plseries"),
  m("fill_null", "fill_null(value)", "Replace nulls.", "plseries"),
  m("unique", "unique()", "Distinct values.", "plseries"),
  m("len", "len()", "Number of values.", "scalar"),
  m("sum", "sum()", "Total.", "scalar"), m("mean", "mean()", "Average.", "scalar"),
  m("min", "min()", "Smallest.", "scalar"), m("max", "max()", "Largest.", "scalar"),
  m("n_unique", "n_unique()", "Distinct values.", "scalar"),
  m("value_counts", "value_counts()", "How often each value occurs.", "pldf"),
  m("to_list", "to_list()", "A plain Python list.", "other"),
  m("to_frame", "to_frame()", "A one-column DataFrame.", "pldf"),
];

const WHEN: Item[] = [m("then", "then(value)", "The value where the condition holds.", "then")];
const THEN: Item[] = [
  m("when", "when(cond)", "Another condition, tried next.", "when"),
  m("otherwise", "otherwise(value)", "The value where no condition held (default: null).", "expr"),
  m("alias", "alias(\"name\")", "Name the column (without otherwise: null where nothing held).", "expr"),
];

// methods whose string arguments are column names: pl.col("…", "…"), df.select("…"), .over("…") …
const COLUMN_ARGS = new Set(["col", "select", "drop", "sort", "group_by", "unique", "drop_nulls", "explode", "unnest",
  "get_column", "over", "exclude"]);

export type Engine = "pandas" | "polars";

const MEMBERS: Partial<Record<T, Item[]>> = {
  df: DF, series: SERIES, str: STR, dt: DT, pd: PD, np: NP, groupby: GROUPBY,
  pl: PL, pldf: PLDF, lazy: LAZY, expr: EXPR, plstr: PLSTR, pldt: PLDT, pllist: PLLIST, plgroupby: PLGROUPBY,
  plseries: PLSERIES, when: WHEN, then: THEN,
};

/** Plain-language list of what's covered, for the guide. */
export const CATALOG = { DataFrame: DF, Series: SERIES, ".str": STR, ".dt": DT, "pd.": PD, "np.": NP, groupby: GROUPBY };
export const POLARS_CATALOG = {
  "pl.": PL, DataFrame: PLDF, LazyFrame: LAZY, "Expressions (pl.col(…))": EXPR, ".str": PLSTR, ".dt": PLDT, ".list": PLLIST,
  group_by: PLGROUPBY, Series: PLSERIES,
};

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
    if (bt === "pldf") return /^["'][^"']*["']$/.test(inner) ? "plseries" : "pldf";
    return bt === "series" || bt === "plseries" ? bt : "other";
  }
  if (expr.endsWith(")")) {
    const o = matchingOpen(expr, expr.length - 1);
    if (o === 0) {  // (pl.col("a") * 2).… : the parenthesised expression's own type
      const inner = expr.slice(1, -1);
      const t = typeOf(inner, vars);
      return t !== "other" ? t : vars.has("pl") && /\bpl\.(col|lit|when|all|len)\(/.test(inner) ? "expr" : "other";
    }
    if (o < 0) return "other";
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

function knownVars(code: string, engine: Engine): Map<string, T> {
  const vars = new Map<string, T>(engine === "polars" ? [["df", "pldf"], ["pl", "pl"], ["np", "np"]] : [["df", "df"], ["pd", "pd"], ["np", "np"]]);
  for (const line of code.split("\n")) {
    const a = /^\s*([A-Za-z_]\w*)\s*=\s*(?!=)(.+?)\s*(#.*)?$/.exec(line);
    if (a) vars.set(a[1], typeOf(a[2], vars));
    const imp = /^\s*import\s+(pandas|numpy|polars)\s+as\s+(\w+)/.exec(line);
    if (imp) vars.set(imp[2], imp[1] === "pandas" ? "pd" : imp[1] === "polars" ? "pl" : "np");
  }
  return vars;
}

const MODULE_DOC: Partial<Record<T, string>> = {
  df: "DataFrame", pldf: "polars DataFrame", lazy: "LazyFrame", series: "Series", plseries: "polars Series", expr: "expression",
  pd: "pandas (pre-imported)", np: "numpy (pre-imported)", pl: "polars (pre-imported)",
};

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

function columnItems(columns: string[], prefix: string, insert: (c: string) => string): Item[] {
  return rank(columns.map((c) => ({ label: c, kind: "column" as Kind, sig: `column "${c}"`, doc: "A column of the data this step receives.", insert: insert(c) })), prefix);
}

/** Completions at `caret`, or null when nothing fits. `force` (Ctrl+Space) also completes bare words. `engine`: what
 * `df` is (a pandas or a Polars DataFrame) and which module is pre-imported (pd or pl). */
export function complete(code: string, caret: number, columns: string[], force = false, engine: Engine = "pandas"): Completion | null {
  const before = code.slice(0, caret);
  const lineStart = before.lastIndexOf("\n") + 1;
  const line = before.slice(lineStart);
  const vars = knownVars(code.slice(0, lineStart), engine);

  // df["… : column names
  const col = /([\w.\]\)]+)\[\s*(["'])([^"'\]]*)$/.exec(line);
  if (col) {
    const t = typeOf(receiverBefore(before, caret - col[0].length + col[1].length), vars);
    if (t === "df" || t === "loc" || t === "pldf") {
      const q = col[2], prefix = col[3], after = code.slice(caret);
      const closing = after.startsWith(q + "]") ? 2 : after.startsWith(q) ? 1 : 0;
      const items = columnItems(columns, prefix, (c) => `${c}${q}]`);
      return items.length ? { items, from: caret - prefix.length, to: caret + closing, prefix } : null;
    }
  }
  // pl.col("… / df.select("a", "… / .over("… : column names as string arguments (Polars)
  const arg = engine === "polars" ? /([\w.\]\)]+)\.([A-Za-z_]\w*)\(((?:\s*["'][^"']*["']\s*,)*)\s*(["'])([^"']*)$/.exec(line) : null;
  if (arg && COLUMN_ARGS.has(arg[2])) {
    const recvT = typeOf(receiverBefore(before, caret - arg[0].length + arg[1].length), vars);
    if (recvT === "pl" ? arg[2] === "col" || arg[2] === "exclude" : ["pldf", "lazy", "expr", "then"].includes(recvT)) {
      const q = arg[4], prefix = arg[5];
      const closing = code.slice(caret).startsWith(q) ? 1 : 0;
      const items = columnItems(columns, prefix, (c) => `${c}${q}`);
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
    if (t === "other" && recv) items = engine === "polars" ? [...EXPR, ...PLDF] : [...DF, ...SERIES]; // unknown receiver: offer both
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
    ...[...vars.entries()].map(([n, t]): Item => ({ label: n, kind: t === "pd" || t === "np" || t === "pl" ? "module" : "variable", sig: n, doc: MODULE_DOC[t] || "variable" })),
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
