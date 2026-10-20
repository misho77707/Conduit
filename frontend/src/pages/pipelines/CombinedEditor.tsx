import { useEffect, useRef, useState } from "react";
import {
  connectionsApi, filesApi, remoteApi, type CombinedIn, type CombinedInputIn, type CombinedPreviewInfo,
  type ConnectionOut, type SourceIn, type TargetIn,
} from "../../lib/api";
import { useToast } from "../../lib/toast";
import { Field } from "../../ui/Field";
import { Input, Select } from "../../ui/Input";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ToggleRow } from "../../ui/Toggle";
import { Icon } from "../../ui/Icon";
import { Button, IconButton } from "../../ui/Button";
import { Modal } from "../../ui/Modal";
import { CodeEditor } from "./CodeEditor";
import { Code, Point } from "./PythonGuide";
import { SourceEditor } from "./SourceTargetEditor";
import { sqlComplete, type SqlTable } from "./sqlComplete";
import { ALIAS_RE, MAX_INPUTS, aliasFrom, emptyInputSource, inputPlace, inputWhat, isTimeType } from "./combined";

type Col = { name: string; type: string };

const MODE_HELP = {
  staged: "Copies every input to disk on the Conduit host, runs the SQL once, then loads the result. Any SQL works, GROUP BY included.",
  stream: "Streams the driver in batches; the other inputs are held in memory. Nothing is copied to disk, but the SQL can't GROUP BY, DISTINCT or use window functions at the top level.",
};

/** The editor of a pipeline's combined source: inputs, mode, the joining SQL and the driver's watermark. */
export function CombinedEditor({ value, onChange, conns, info, targetMode }: {
  value: CombinedIn; onChange: (c: CombinedIn) => void; conns: ConnectionOut[];
  info: CombinedPreviewInfo | null; // from the latest preview
  targetMode: TargetIn["mode"];
}) {
  const latest = useRef(value);
  latest.current = value;
  const [inspected, setInspected] = useState<Record<string, Col[]>>({}); // every column of each input, by alias
  const [guide, setGuide] = useState(false);
  // stable keys for the input cards, so a card's open state follows its input when inputs move
  const ids = useRef<number[]>([]);
  const nextId = useRef(0);
  if (ids.current.length !== value.inputs.length) ids.current = value.inputs.map(() => nextId.current++);

  const update = (fn: (c: CombinedIn) => CombinedIn) => onChange(fn(latest.current));

  // columns the SQL can use: what the input was inspected with (narrowed to its column subset), else the preview's
  const columnsOf = (inp: CombinedInputIn): Col[] => {
    const all = inspected[inp.alias];
    if (all) return inp.columns?.length ? all.filter((c) => inp.columns!.includes(c.name)) : all;
    return info?.inputs.find((x) => x.alias === inp.alias)?.columns || [];
  };
  const tables: SqlTable[] = value.inputs.map((i) => ({ alias: i.alias, columns: columnsOf(i) }));
  const driver = value.inputs.find((i) => i.alias === value.driver) || null;
  const driverCols = driver ? columnsOf(driver) : [];
  const wmType = driverCols.find((c) => c.name === value.watermark?.column)?.type;
  const wmTime = isTimeType(wmType);

  function setInput(idx: number, next: CombinedInputIn) {
    update((c) => {
      const old = c.inputs[idx];
      const inputs = c.inputs.map((x, i) => (i === idx ? next : x));
      return { ...c, inputs, driver: c.driver === old.alias ? next.alias : c.driver };
    });
  }
  function move(idx: number, by: -1 | 1) {
    update((c) => {
      const inputs = [...c.inputs];
      [inputs[idx], inputs[idx + by]] = [inputs[idx + by], inputs[idx]];
      const k = ids.current;
      [k[idx], k[idx + by]] = [k[idx + by], k[idx]];
      return { ...c, inputs };
    });
  }
  function remove(idx: number) {
    update((c) => {
      const gone = c.inputs[idx].alias;
      const inputs = c.inputs.filter((_, i) => i !== idx);
      ids.current = ids.current.filter((_, i) => i !== idx);
      const needsDriver = c.mode === "stream" || !!c.watermark;
      const drv = c.driver === gone ? (needsDriver ? inputs[0].alias : null) : c.driver;
      return { ...c, inputs, driver: drv, watermark: c.driver === gone ? null : c.watermark };
    });
  }
  function add() {
    ids.current = [...ids.current, nextId.current++];
    update((c) => ({
      ...c,
      inputs: [...c.inputs, { alias: aliasFrom(`input${c.inputs.length + 1}`, c.inputs.map((i) => i.alias)), source: emptyInputSource(conns), columns: null }],
    }));
  }
  function toggleDriver(alias: string) {
    update((c) => {
      if (c.driver !== alias) return { ...c, driver: alias, watermark: c.watermark ? { ...c.watermark, column: "" } : null };
      return c.mode === "stream" || c.watermark ? c : { ...c, driver: null }; // stream mode / a watermark need one
    });
  }
  function setMode(mode: CombinedIn["mode"]) {
    update((c) => ({ ...c, mode, driver: mode === "stream" && !c.driver ? c.inputs[0].alias : c.driver }));
  }
  function setWatermark(on: boolean) {
    update((c) => (on
      ? { ...c, driver: c.driver || c.inputs[0].alias, watermark: { column: "", overlap: 0 } }
      : { ...c, watermark: null }));
  }

  function columnsOfAlias(alias: string, cols: Col[] | null) {
    setInspected((cur) => {
      const live = new Set(latest.current.inputs.map((i) => i.alias));
      const next = Object.fromEntries(Object.entries(cur).filter(([a]) => live.has(a) && a !== alias));
      return cols ? { ...next, [alias]: cols } : next;
    });
  }

  // An input still named input2 takes its table / file name once that is found, if the SQL doesn't use it yet
  function found(alias: string, name: string) {
    const c = latest.current;
    if (!/^input\d+$/.test(alias) || !name) return;
    const word = new RegExp(`(?<![A-Za-z0-9_])${alias}(?![A-Za-z0-9])`, "g"); // input2, input2.id, input2_id
    if (word.test(c.sql.replace(/--.*$/gm, ""))) return;
    const fresh = aliasFrom(name, c.inputs.map((i) => i.alias).filter((a) => a !== alias));
    if (fresh === alias || /^input/.test(fresh) || !ALIAS_RE.test(fresh)) return;
    onChange({
      ...c,
      inputs: c.inputs.map((i) => (i.alias === alias ? { ...i, alias: fresh } : i)),
      sql: c.sql.replace(word, fresh),
      driver: c.driver === alias ? fresh : c.driver,
    });
  }

  function useExample(sql: string) {
    if (value.sql.trim() && !/^SELECT \*\nFROM \w+\n(--.*\n?)?$/.test(value.sql) && !confirm("Replace the current SQL with this example?")) return;
    update((c) => ({ ...c, sql }));
    setGuide(false);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <SegmentedControl small options={[{ id: "staged", label: "Staged" }, { id: "stream", label: "Stream" }]} value={value.mode} onChange={setMode} />
        <span style={{ fontSize: 12, color: "var(--text-3)" }}>{MODE_HELP[value.mode]}</span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)", flex: 1 }}>Inputs · {value.inputs.length} of {MAX_INPUTS}</span>
          <Button variant="ghost" icon="add" height={26} padX={8} disabled={value.inputs.length >= MAX_INPUTS} onClick={add} style={{ color: "var(--accent)", fontSize: 12 }}>Add input</Button>
        </div>
        {value.inputs.map((inp, i) => (
          <InputCard
            key={ids.current[i]} inp={inp} conns={conns} first={i === 0} last={i === value.inputs.length - 1} removable={value.inputs.length > 2}
            isDriver={value.driver === inp.alias} mode={value.mode}
            known={inspected[inp.alias] || (inp.columns?.length ? [] : info?.inputs.find((x) => x.alias === inp.alias)?.columns) || []}
            onChange={(next) => setInput(i, next)} onMove={(by) => move(i, by)} onRemove={() => remove(i)}
            onDriver={() => toggleDriver(inp.alias)} onColumns={columnsOfAlias} onFound={found}
          />
        ))}
        <span style={{ fontSize: 11, color: "var(--text-3)" }}>
          {value.mode === "stream"
            ? "The driver is streamed; every other input is read into memory first (capped in rows and MB)."
            : "Mark a driver to read only its new rows with a watermark; otherwise every run reads every input in full."}
        </span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)", flex: 1 }}>SQL · DuckDB, each input's alias is a table</span>
          <Button height={26} padX={8} icon="menu_book" style={{ fontSize: 12 }} onClick={() => setGuide(true)}>Examples</Button>
        </div>
        <CodeEditor code={value.sql} onChange={(sql) => update((c) => ({ ...c, sql }))} lang="sql" suggest
          completer={(code, caret, force) => sqlComplete(code, caret, tables, force)} />
        <span style={{ fontSize: 11, color: "var(--text-3)" }}>
          Suggestions: aliases, then columns after <code>alias.</code> · Ctrl+Space anywhere
        </span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <ToggleRow on={!!value.watermark} onClick={() => setWatermark(!value.watermark)} title="Only new rows of the driver (watermark)"
          sub={`Each run reads the rows of ${driver?.alias || "the driver"} above the highest value loaded so far. The value is saved only when a run succeeds.`} />
        {value.watermark && (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 150px", gap: 10, paddingLeft: 38 }}>
            <Field label={`Column of ${driver?.alias || "the driver"}`}>
              <Select value={value.watermark.column} onChange={(e) => update((c) => ({ ...c, watermark: { ...c.watermark!, column: e.target.value } }))}>
                <option value="">Choose…</option>
                {value.watermark.column && !driverCols.some((c) => c.name === value.watermark!.column) && <option value={value.watermark.column}>{value.watermark.column}</option>}
                {driverCols.map((c) => <option key={c.name} value={c.name}>{c.name} · {c.type}</option>)}
              </Select>
            </Field>
            <Field label={wmTime ? "Overlap · minutes" : "Overlap · column units"}>
              <Input type="number" min={0} value={wmTime ? value.watermark.overlap / 60 : value.watermark.overlap}
                onChange={(e) => {
                  const n = Math.max(0, Number(e.target.value) || 0);
                  update((c) => ({ ...c, watermark: { ...c.watermark!, overlap: wmTime ? n * 60 : n } }));
                }} />
            </Field>
            <span style={{ gridColumn: "1 / -1", fontSize: 11, color: value.watermark.overlap > 0 && targetMode !== "upsert" ? "var(--warn)" : "var(--text-3)" }}>
              {value.watermark.overlap > 0 && targetMode !== "upsert"
                ? `The overlap re-reads rows that were already loaded, and the target loads with ${targetMode}: they will be loaded twice. Pair an overlap with Upsert.`
                : "The overlap re-reads rows just below the saved value, to catch rows that arrive late. Pair it with Upsert on the target."}
            </span>
          </div>
        )}
      </div>

      {guide && <CombinedGuide onClose={() => setGuide(false)} onUse={useExample} tables={tables} />}
    </div>
  );
}

function sourceKey(s: SourceIn): string {
  return JSON.stringify({ ...s, where: undefined }); // a WHERE doesn't change the columns
}

function InputCard({ inp, conns, first, last, removable, isDriver, mode, known, onChange, onMove, onRemove, onDriver, onColumns, onFound }: {
  inp: CombinedInputIn; conns: ConnectionOut[]; first: boolean; last: boolean; removable: boolean; isDriver: boolean;
  mode: CombinedIn["mode"]; known: Col[];
  onChange: (v: CombinedInputIn) => void; onMove: (by: -1 | 1) => void; onRemove: () => void; onDriver: () => void;
  onColumns: (alias: string, cols: Col[] | null) => void; // every column of the input, under its current alias
  onFound: (alias: string, name: string) => void; // its table / file was found
}) {
  const what = inputWhat(inp.source, conns);
  const [open, setOpen] = useState(!what);
  const [draft, setDraft] = useState("");
  const [cols, setCols] = useState<Col[] | null>(null);
  const key = sourceKey(inp.source);
  const aliasRef = useRef(inp.alias);
  aliasRef.current = inp.alias;
  const cb = useRef({ onColumns, onFound });
  cb.current = { onColumns, onFound };

  useEffect(() => { cb.current.onColumns(inp.alias, cols); }, [inp.alias, cols]);

  useEffect(() => { // every column of the input, for the column picker and the SQL suggestions
    const s = inp.source;
    let gone = false;
    const t = setTimeout(() => {
      const got: Promise<Col[]> | null =
        s.remote?.path.trim() ? remoteApi.inspect(s.remote, 5).then((r) => r.columns)
        : s.file?.path.trim() ? filesApi.inspect(s.file.path, 5, { header: s.file.header, skip_rows: s.file.skip_rows, delimiter: s.file.delimiter }).then((r) => r.columns)
        : s.connection_id && s.table?.trim() ? connectionsApi.inspect(s.connection_id, s.table.trim()).then((r) => r.columns)
        : null;
      if (!got) { setCols(null); return; }
      got.then((all) => {
        if (gone) return;
        setCols(all.map((c) => ({ name: c.name, type: c.type })));
        cb.current.onFound(aliasRef.current, inputWhat(s, conns));
      }).catch(() => { if (!gone) setCols(null); });
    }, 600);
    return () => { gone = true; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const aliasOk = ALIAS_RE.test(inp.alias);
  const chosen = inp.columns || [];
  const addColumn = (name: string) => {
    const n = name.trim();
    if (n && !chosen.includes(n)) onChange({ ...inp, columns: [...chosen, n] });
    setDraft("");
  };
  const listId = `cols-${inp.alias}`;
  const driverTitle = mode === "stream"
    ? "The driver is streamed batch by batch; the other inputs are lookups held in memory."
    : "The driver is the input a watermark reads only the new rows of.";

  return (
    <div style={{ border: `1px solid ${isDriver ? "var(--accent)" : "var(--border)"}`, borderRadius: 7, background: "var(--surface)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 8px" }}>
        <IconButton icon={open ? "expand_more" : "chevron_right"} title={open ? "Collapse" : "Edit this input"} onClick={() => setOpen((o) => !o)} />
        <Input mono value={inp.alias} onChange={(e) => onChange({ ...inp, alias: e.target.value })} title="The name the SQL uses for this input"
          style={{ width: 130, height: 28, borderColor: aliasOk ? undefined : "var(--err)" }} />
        <span title={`${what || "nothing chosen yet"} · ${inputPlace(inp.source, conns)}`} onClick={() => setOpen((o) => !o)}
          style={{ flex: 1, minWidth: 0, fontFamily: "'Geist Mono',monospace", fontSize: 11, color: what ? "var(--text-2)" : "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", cursor: "pointer" }}>
          {what ? `${what} · ${inputPlace(inp.source, conns)}` : "choose a source"}
        </span>
        <button onClick={onDriver} title={driverTitle}
          style={{ height: 24, padding: "0 8px", borderRadius: 5, border: `1px solid ${isDriver ? "var(--accent)" : "var(--border-2)"}`, background: isDriver ? "var(--accent-soft)" : "transparent", color: isDriver ? "var(--accent)" : "var(--text-3)", fontSize: 11, fontWeight: 500, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
          <Icon name="bolt" style={{ fontSize: 13 }} />Driver
        </button>
        <IconButton icon="arrow_upward" title="Move up" disabled={first} onClick={() => onMove(-1)} />
        <IconButton icon="arrow_downward" title="Move down" disabled={last} onClick={() => onMove(1)} />
        <IconButton icon="delete" title={removable ? "Remove this input" : "A combined source needs at least two inputs"} disabled={!removable} onClick={onRemove} style={{ color: "var(--text-3)" }} />
      </div>
      {open && (
        <div style={{ padding: "4px 12px 12px", display: "flex", flexDirection: "column", gap: 12, borderTop: "1px solid var(--border)" }}>
          <div style={{ paddingTop: 8 }}>
            <SourceEditor value={inp.source} onChange={(source) => onChange({ ...inp, source })} conns={conns} asInput />
          </div>
          <Field label="Columns · optional, read only these (all when empty)">
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              {chosen.map((c) => (
                <span key={c} style={{ height: 24, padding: "0 8px", borderRadius: 5, border: "1px solid var(--border-2)", fontFamily: "'Geist Mono',monospace", fontSize: 11, display: "inline-flex", alignItems: "center", gap: 4 }}>
                  {c}
                  <button onClick={(e) => { e.preventDefault(); const rest = chosen.filter((x) => x !== c); onChange({ ...inp, columns: rest.length ? rest : null }); }}
                    style={{ border: "none", background: "none", color: "var(--text-3)", cursor: "pointer", padding: 0, display: "flex" }}><Icon name="close" style={{ fontSize: 13 }} /></button>
                </span>
              ))}
              <input list={listId} value={draft} onChange={(e) => {
                const v = e.target.value;
                if (known.some((k) => k.name === v)) addColumn(v); else setDraft(v); // picked from the list
              }} onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addColumn(draft))}
                placeholder={chosen.length ? "add a column" : "all columns"}
                style={{ height: 24, width: 150, padding: "0 6px", borderRadius: 5, border: "1px solid var(--border-2)", background: "var(--surface)", fontSize: 11, fontFamily: "'Geist Mono',monospace" }} />
              <datalist id={listId}>{known.filter((k) => !chosen.includes(k.name)).map((k) => <option key={k.name} value={k.name}>{k.type}</option>)}</datalist>
            </div>
          </Field>
        </div>
      )}
    </div>
  );
}

const EXAMPLES: { title: string; about: string; sql: string }[] = [
  {
    title: "Enrich rows with a lookup",
    about: "Every event, with columns from a small users table. Works in both modes: in stream mode, mark events as the driver and users is held in memory.",
    sql: `SELECT e.*, u.name AS user_name, u.country
FROM events AS e
LEFT JOIN users AS u ON u.id = e.user_id`,
  },
  {
    title: "Daily totals across two databases",
    about: "An aggregate needs staged mode: the inputs are copied first and the SQL runs once over all of them. Load with Replace, or Upsert on (day, country), so a re-run doesn't add the totals twice.",
    sql: `SELECT CAST(e.created_at AS DATE) AS day, u.country,
       count(*) AS events, sum(e.amount) AS amount
FROM events AS e
JOIN users AS u ON u.id = e.user_id
GROUP BY ALL`,
  },
  {
    title: "Join two big tables",
    about: "Both sides are large, so use staged mode: both are copied to disk on the Conduit host and joined there. Narrow each input with its WHERE and Columns to copy less.",
    sql: `SELECT o.id AS order_id, o.created_at, o.total,
       p.status AS payment_status, p.paid_at
FROM orders AS o
LEFT JOIN payments AS p ON p.order_id = o.id`,
  },
];

const k = (t: string) => <code style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text)" }}>{t}</code>;

function CombinedGuide({ onClose, onUse, tables }: { onClose: () => void; onUse: (sql: string) => void; tables: SqlTable[] }) {
  const toast = useToast();
  const copy = (text: string) => navigator.clipboard?.writeText(text).then(() => toast("ok", "Copied")).catch(() => {});
  return (
    <Modal width={820} title="Combining inputs" sub="One DuckDB SELECT over the inputs of a combined source" onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Point icon="table">Each input is a table named by its alias{tables.length ? <>: {tables.map((t, i) => <span key={t.alias}>{i ? ", " : ""}{k(t.alias)}</span>)}</> : null}. Write one SELECT over them; the columns it returns are what the next step (or the target) gets. Give duplicate names distinct aliases ({k("u.id AS user_id")}).</Point>
        <Point icon="key">Join keys must be the same kind of value on both sides. When one side is text and the other a number, cast one: {k("ON u.id = CAST(e.user_id AS BIGINT)")}.</Point>
        <Point icon="table_rows">The preview reads only the first rows of each input, so some matches may be missing from it; the run reads everything.</Point>
        <Point icon="view_agenda">Steps after the source still run per batch on the joined rows.</Point>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-3)" }}>Table and column names in the examples are placeholders: change them to your aliases. &ldquo;Use this&rdquo; replaces the SQL.</div>
      {EXAMPLES.map((ex) => (
        <div key={ex.title} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600 }}>{ex.title}</div>
              <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 2 }}>{ex.about}</div>
            </div>
            <Button height={28} padX={10} icon="content_copy" style={{ fontSize: 12 }} onClick={() => copy(ex.sql)}>Copy</Button>
            <Button height={28} padX={10} icon="input" variant="primary" style={{ fontSize: 12 }} onClick={() => onUse(ex.sql)}>Use this</Button>
          </div>
          <Code text={ex.sql} lang="sql" />
        </div>
      ))}
    </Modal>
  );
}

/** The preview's per-input row counts, the join's size and its notes / warnings (Builder's preview panel). */
export function CombinedPreviewStrip({ info }: { info: CombinedPreviewInfo }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: "10px 14px", borderBottom: "1px solid var(--border)", fontSize: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        {info.inputs.map((i) => (
          <span key={i.alias} title={i.capped ? "Only these first rows were read for the preview: matches beyond them are missing here." : `${i.columns.length} columns`}
            style={{ height: 22, padding: "0 7px", borderRadius: 5, border: `1px solid ${i.capped ? "var(--warn)" : "var(--border-2)"}`, color: i.capped ? "var(--warn)" : "var(--text-2)", display: "inline-flex", alignItems: "center", gap: 5, fontFamily: "'Geist Mono',monospace", fontSize: 11 }}>
            {i.capped && <Icon name="content_cut" style={{ fontSize: 13 }} />}{i.alias} {i.rows.toLocaleString()}{i.capped ? "+" : ""} rows
          </span>
        ))}
        <Icon name="arrow_forward" style={{ fontSize: 15, color: "var(--text-3)" }} />
        <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text)" }}>join {info.join_rows.toLocaleString()} rows</span>
        <span style={{ fontSize: 11, color: "var(--text-3)" }}>· {info.mode}</span>
      </div>
      {info.key_mismatches.map((m) => (
        <div key={m} style={{ display: "flex", gap: 6, color: "var(--warn)" }}><Icon name="warning" style={{ fontSize: 15, flexShrink: 0 }} /><span style={{ color: "var(--text)" }}>{m}</span></div>
      ))}
      {info.notes.map((n) => (
        <div key={n} style={{ display: "flex", gap: 6, color: "var(--text-3)" }}><Icon name="info" style={{ fontSize: 15, flexShrink: 0 }} /><span style={{ color: "var(--text-2)" }}>{n}</span></div>
      ))}
    </div>
  );
}
