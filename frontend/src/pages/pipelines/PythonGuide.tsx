import { useState, type ReactNode } from "react";
import { highlight } from "../../lib/highlight";
import { useToast } from "../../lib/toast";
import { Modal } from "../../ui/Modal";
import { Button } from "../../ui/Button";
import { Icon } from "../../ui/Icon";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { CATALOG } from "./pyComplete";

type Tab = "basics" | "examples" | "reference";

const EXAMPLES: { title: string; about: string; code: string }[] = [
  {
    title: "Add a calculated column",
    about: "Arithmetic on whole columns at once (fast, no loops).",
    code: `def transform(df):
    df["total"] = df["qty"] * df["unit_price"]
    df["total_with_vat"] = (df["total"] * 1.18).round(2)
    return df`,
  },
  {
    title: "Keep only some rows",
    about: "A boolean condition selects rows. Combine conditions with & (and), | (or), ~ (not) and wrap each in brackets.",
    code: `def transform(df):
    paid = df["status"] == "paid"
    positive = df["amount"] > 0
    return df[paid & positive]`,
  },
  {
    title: "Clean up text",
    about: "The .str accessor works on every value of a text column; missing values stay missing.",
    code: `def transform(df):
    df["email"] = df["email"].str.strip().str.lower()
    df["name"] = df["name"].str.title()
    df["phone"] = df["phone"].str.replace(r"[^0-9+]", "", regex=True)
    return df`,
  },
  {
    title: "Parse dates and numbers from text",
    about: "errors=\"coerce\" turns values that cannot be parsed into NaT / NaN instead of failing the whole batch.",
    code: `def transform(df):
    df["posted_at"] = pd.to_datetime(df["posted_at"], format="%d.%m.%Y %H:%M", errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"].str.replace(",", "."), errors="coerce")
    return df`,
  },
  {
    title: "If / else column (CASE WHEN)",
    about: "np.where for one condition, np.select for several (first match wins).",
    code: `def transform(df):
    df["size"] = np.where(df["amount"] >= 1000, "large", "small")
    df["tier"] = np.select(
        [df["amount"] >= 10000, df["amount"] >= 1000],
        ["gold", "silver"],
        default="bronze",
    )
    return df`,
  },
  {
    title: "Map codes to labels",
    about: "A dict lookup per value; codes missing from the dict become NaN unless you fillna.",
    code: `STATUS = {"A": "active", "I": "inactive", "D": "deleted"}

def transform(df):
    df["status"] = df["status"].map(STATUS).fillna("unknown")
    return df`,
  },
  {
    title: "Split one column into several",
    about: "expand=True returns columns; n=1 splits only at the first space.",
    code: `def transform(df):
    df[["first_name", "last_name"]] = df["full_name"].str.split(" ", n=1, expand=True)
    df["year"] = df["created"].dt.year
    df["day"] = df["created"].dt.date
    return df`,
  },
  {
    title: "Fill missing values",
    about: "Per-column defaults. Use nullable types (\"Int64\", \"boolean\", \"string\") to keep real NULLs instead.",
    code: `def transform(df):
    df = df.fillna({"country": "GE", "discount": 0})
    df["quantity"] = df["quantity"].astype("Int64")  # integer column that may contain NULLs
    return df`,
  },
  {
    title: "Rename, reorder and drop columns",
    about: "The columns you return are exactly the columns the next step (or the target table) gets.",
    code: `def transform(df):
    df = df.rename(columns={"acct": "account_id", "amt": "amount"})
    df = df.drop(columns=["internal_note"])
    return df[["account_id", "amount", "currency"]]`,
  },
  {
    title: "Pull fields out of a JSON column",
    about: "Any module can be imported. json_normalize flattens nested objects into columns.",
    code: `import json

def transform(df):
    payload = df["payload"].apply(lambda s: json.loads(s) if isinstance(s, str) else {})
    fields = pd.json_normalize(payload.tolist())[["user.id", "user.country"]]
    fields.columns = ["user_id", "user_country"]
    return pd.concat([df.drop(columns=["payload"]).reset_index(drop=True), fields], axis=1)`,
  },
  {
    title: "Row-by-row logic",
    about: "When vectorised code gets awkward, apply a function per row. Works, but is much slower on large batches.",
    code: `def label(row):
    if row["refunded"]:
        return "refund"
    if row["amount"] > 1000 and row["country"] != "GE":
        return "review"
    return "ok"

def transform(df):
    df["flag"] = df.apply(label, axis=1)
    return df`,
  },
  {
    title: "Remove duplicates (per batch)",
    about: "Only duplicates inside the same batch are seen. For table-wide uniqueness, load with Upsert on a key instead.",
    code: `def transform(df):
    df = df.sort_values("modified", ascending=False)
    return df.drop_duplicates(subset=["id"], keep="first")`,
  },
];

const CHEATS: [string, string][] = [
  ["Pick columns", `df[["id", "amount"]]`],
  ["New column", `df["total"] = df["qty"] * df["price"]`],
  ["Filter rows", `df = df[df["amount"] > 0]`],
  ["Rename", `df = df.rename(columns={"old": "new"})`],
  ["Drop columns", `df = df.drop(columns=["tmp"])`],
  ["Missing → value", `df["x"] = df["x"].fillna(0)`],
  ["Is missing?", `df["x"].isna()`],
  ["Trim + lowercase", `df["e"] = df["e"].str.strip().str.lower()`],
  ["Text contains", `df[df["name"].str.contains("ltd", case=False, na=False)]`],
  ["Text → date", `pd.to_datetime(df["d"], format="%Y-%m-%d", errors="coerce")`],
  ["Text → number", `pd.to_numeric(df["n"], errors="coerce")`],
  ["Date parts", `df["d"].dt.year, df["d"].dt.date, df["d"].dt.strftime("%Y-%m")`],
  ["If / else", `np.where(df["amt"] > 0, "in", "out")`],
  ["Value in list", `df[df["country"].isin(["GE", "AM"])]`],
  ["Integer with NULLs", `df["id"].astype("Int64")`],
  ["Round", `df["amt"].round(2)`],
  ["Constant column", `df["source"] = "crm"`],
  ["Concatenate text", `df["full"] = df["first"] + " " + df["last"]`],
];

function Code({ text }: { text: string }) {
  return (
    <div style={{ background: "var(--code-bg)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 0", fontFamily: "'Geist Mono',monospace", fontSize: 12, lineHeight: "19px", overflowX: "auto" }}>
      {highlight(text, "py").map((ln) => (
        <div key={ln.n} style={{ whiteSpace: "pre", padding: "0 12px", minHeight: 19 }}>
          {ln.segs.map((sg, i) => <span key={i} style={{ color: sg.c }}>{sg.t}</span>)}
        </div>
      ))}
    </div>
  );
}

function Point({ icon, children }: { icon: string; children: ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
      <Icon name={icon} style={{ fontSize: 17, color: "var(--accent)", marginTop: 1 }} />
      <div style={{ color: "var(--text-2)", lineHeight: 1.55 }}>{children}</div>
    </div>
  );
}

const k = (t: string) => <code style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text)" }}>{t}</code>;

export function PythonGuide({ onClose, onUse, columns }: { onClose: () => void; onUse: (code: string) => void; columns: string[] }) {
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("basics");
  const [q, setQ] = useState("");

  const copy = (text: string) => navigator.clipboard?.writeText(text).then(() => toast("ok", "Copied")).catch(() => {});
  const ql = q.trim().toLowerCase();

  return (
    <Modal width={860} title="Python transform guide" sub="pandas inside a pipeline step" onClose={onClose}>
      <SegmentedControl options={[{ id: "basics", label: "Basics" }, { id: "examples", label: "Examples" }, { id: "reference", label: "Reference" }]} value={tab} onChange={setTab} />

      {tab === "basics" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Code text={`def transform(df):\n    # df: a pandas DataFrame with one batch of rows\n    ...\n    return df  # must return a DataFrame`} />
          <Point icon="function">Define {k("transform(df)")} and return a DataFrame. The columns you return become the next step&rsquo;s input, or the target table&rsquo;s columns if this is the last step.</Point>
          <Point icon="view_agenda">It runs <b>once per batch</b> (the job&rsquo;s chunk size, 50,000 rows by default), never over the whole table at once. Sorting, {k("groupby")}, {k("drop_duplicates")} and running totals only see the current batch.</Point>
          <Point icon="extension">{k("pd")} (pandas) and {k("np")} (numpy) are already imported. Anything else installed on the worker can be imported as usual: {k("import json, re, datetime")}.</Point>
          <Point icon="table_rows">The preview runs your code on a sample of real rows, so errors show up right away: the step turns red with the Python exception, and the table shows the data just before this step.</Point>
          <Point icon="data_object">Types: use nullable types to keep NULLs: {k("\"Int64\"")}, {k("\"boolean\"")}, {k("\"string\"")}. Datetime columns load as TIMESTAMP (or TIMESTAMPTZ when time-zone aware). A column that mixes values of different types (e.g. numbers and text) cannot be converted, so cast it first.</Point>
          <Point icon="speed">Prefer whole-column operations ({k("df[\"a\"] * 2")}, {k(".str")}, {k(".dt")}, {k("np.where")}) over {k("apply")} and Python loops: they are often 100× faster.</Point>
          <Point icon="warning">Not sandboxed: the code runs with the worker&rsquo;s own permissions (files, network). {k("print()")} output is not shown anywhere.</Point>
          <Point icon="keyboard">In the editor, suggestions appear after {k("df.")}, {k("df[\"")}, {k(".str.")}, {k(".dt.")}, {k("pd.")} and {k("np.")}. Press {k("Ctrl+Space")} for suggestions anywhere, ↑ ↓ to choose, Enter or Tab to insert, Esc to close. Tab indents and Enter keeps the indentation.</Point>
          {columns.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Columns available here (from the latest preview)</span>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {columns.map((c) => <span key={c} style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, padding: "2px 7px", borderRadius: 5, border: "1px solid var(--border)", color: "var(--text-2)" }}>{c}</span>)}
              </div>
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span style={{ fontWeight: 600 }}>Cheat sheet</span>
            <div style={{ border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
              {CHEATS.map(([what, code]) => (
                <div key={what} className="hover-surface2" onClick={() => copy(code)} title="Click to copy" style={{ display: "grid", gridTemplateColumns: "170px 1fr 20px", gap: 12, padding: "6px 12px", borderBottom: "1px solid var(--border)", alignItems: "center", cursor: "copy" }}>
                  <span style={{ color: "var(--text-2)", fontSize: 12 }}>{what}</span>
                  <code style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, overflowWrap: "anywhere" }}>{code}</code>
                  <Icon name="content_copy" style={{ fontSize: 14, color: "var(--text-3)" }} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {tab === "examples" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <div style={{ fontSize: 12, color: "var(--text-3)" }}>Column names in the examples are placeholders: change them to yours. &ldquo;Use this&rdquo; replaces the step&rsquo;s code.</div>
          {EXAMPLES.map((ex) => (
            <div key={ex.title} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600 }}>{ex.title}</div>
                  <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 2 }}>{ex.about}</div>
                </div>
                <Button height={28} padX={10} icon="content_copy" style={{ fontSize: 12 }} onClick={() => copy(ex.code)}>Copy</Button>
                <Button height={28} padX={10} icon="input" variant="primary" style={{ fontSize: 12 }} onClick={() => onUse(ex.code)}>Use this</Button>
              </div>
              <Code text={ex.code} />
            </div>
          ))}
        </div>
      )}

      {tab === "reference" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ height: 32, display: "flex", alignItems: "center", gap: 6, padding: "0 10px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", color: "var(--text-3)" }}>
            <Icon name="search" style={{ fontSize: 16 }} />
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search methods, e.g. date, strip, missing" style={{ flex: 1, minWidth: 0, border: "none", background: "transparent", outline: "none", color: "var(--text)" }} />
          </div>
          {Object.entries(CATALOG).map(([group, items]) => {
            const shown = items.filter((i) => !ql || `${i.label} ${i.sig || ""} ${i.doc || ""}`.toLowerCase().includes(ql));
            if (!shown.length) return null;
            return (
              <div key={group} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <span style={{ fontWeight: 600 }}>{group} <span style={{ fontWeight: 400, fontSize: 12, color: "var(--text-3)" }}>{GROUP_NOTE[group]}</span></span>
                <div style={{ border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
                  {shown.map((i) => (
                    <div key={i.label} style={{ display: "grid", gridTemplateColumns: "minmax(200px,300px) 1fr", gap: 12, padding: "6px 12px", borderBottom: "1px solid var(--border)", alignItems: "baseline" }}>
                      <code style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, overflowWrap: "anywhere" }}>{i.sig || i.label}</code>
                      <span style={{ fontSize: 12, color: "var(--text-2)" }}>{i.doc}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

const GROUP_NOTE: Record<string, string> = {
  DataFrame: "df.…  the whole batch",
  Series: "df[\"col\"].…  one column",
  ".str": "df[\"col\"].str.…  text columns",
  ".dt": "df[\"col\"].dt.…  date/time columns",
  "pd.": "pandas functions",
  "np.": "numpy functions",
  groupby: "df.groupby(\"col\").…  per batch",
};
