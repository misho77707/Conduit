import { useState, type ReactNode } from "react";
import type { StepIn } from "../../lib/api";
import { LOGICAL_TYPES } from "../../lib/constants";
import { Icon } from "../../ui/Icon";
import { Input, Select, TextArea } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { ToggleRow } from "../../ui/Toggle";
import { CodeEditor } from "./CodeEditor";
import { PythonGuide } from "./PythonGuide";
import { defaultConfig } from "./types";

function Row({ children }: { children: ReactNode }) {
  return <div style={{ display: "flex", gap: 8, alignItems: "center" }}>{children}</div>;
}
function RemoveBtn({ onClick }: { onClick: () => void }) {
  return <button onClick={onClick} style={{ width: 28, height: 28, flexShrink: 0, border: "none", background: "transparent", color: "var(--text-3)", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}><Icon name="close" style={{ fontSize: 16 }} /></button>;
}

// Local array state, seeded once from the incoming {key: value} mapping. Needed because a mapping
// object can't hold an in-progress empty key: deriving rows straight from it each render would make
// a freshly added blank row (key "") vanish the instant it's created (Object.fromEntries collapses
// it away). Only the *outgoing* mapping filters out blank rows; the on-screen rows keep everything
// the user has typed, including duplicates and blanks, until they fill or remove them.
function PairListEditor({
  initial, onChange, colHead, valHead, colPlaceholder, valPlaceholder, addLabel, note, valueIsType,
}: {
  initial: Record<string, string>; onChange: (m: Record<string, string>) => void;
  colHead: string; valHead: string; colPlaceholder: string; valPlaceholder?: string; addLabel: string; note?: string; valueIsType?: boolean;
}) {
  const [pairs, setPairs] = useState<[string, string][]>(() => Object.entries(initial));

  function commit(next: [string, string][]) {
    setPairs(next);
    onChange(Object.fromEntries(next.filter(([a, b]) => a && b)));
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 28px", gap: 8, fontSize: 12, fontWeight: 500, color: "var(--text-3)" }}><span>{colHead}</span><span>{valHead}</span><span /></div>
      {pairs.map(([a, b], i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr 28px", gap: 8, alignItems: "center" }}>
          <Input value={a} onChange={(e) => { const n = [...pairs]; n[i] = [e.target.value, b]; commit(n); }} placeholder={colPlaceholder} />
          {valueIsType ? (
            <Select value={b || LOGICAL_TYPES[0]} onChange={(e) => { const n = [...pairs]; n[i] = [a, e.target.value]; commit(n); }}>
              {LOGICAL_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </Select>
          ) : (
            <Input value={b} onChange={(e) => { const n = [...pairs]; n[i] = [a, e.target.value]; commit(n); }} placeholder={valPlaceholder} />
          )}
          <RemoveBtn onClick={() => commit(pairs.filter((_, j) => j !== i))} />
        </div>
      ))}
      <Button variant="ghost" icon="add" height={28} padX={8} onClick={() => setPairs([...pairs, ["", valueIsType ? LOGICAL_TYPES[0] : ""]])} style={{ alignSelf: "flex-start", color: "var(--accent)", fontSize: 12 }}>{addLabel}</Button>
      {note && <div style={{ fontSize: 12, color: "var(--text-3)" }}>{note}</div>}
    </div>
  );
}

export function StepEditor({ step, onChange, inputColumns }: { step: StepIn; onChange: (config: Record<string, unknown>) => void; inputColumns: string[] }) {
  const c = step.config as Record<string, any>;

  if (step.kind === "rename") {
    return <PairListEditor initial={c.mapping || {}} onChange={(m) => onChange({ mapping: m })} colHead="From" valHead="To" colPlaceholder="source column" valPlaceholder="new name" addLabel="Add rename" note="Unmapped columns pass through unchanged." />;
  }

  if (step.kind === "cast") {
    return <PairListEditor initial={c.mapping || {}} onChange={(m) => onChange({ mapping: m })} colHead="Column" valHead="To type" colPlaceholder="column" addLabel="Add cast" valueIsType />;
  }

  if (step.kind === "filter") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <TextArea rows={2} value={c.expr || ""} onChange={(e) => onChange({ expr: e.target.value })} placeholder="status = 'settled' AND amount > 0" />
        <div style={{ fontSize: 12, color: "var(--text-3)" }}>A raw SQL boolean expression. Rows where it's true are kept.</div>
      </div>
    );
  }

  if (step.kind === "derive") {
    return (
      <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 12 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}><span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>New column</span><Input value={c.column || ""} onChange={(e) => onChange({ ...c, column: e.target.value })} placeholder="is_large" /></div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}><span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Expression</span><Input value={c.expr || ""} onChange={(e) => onChange({ ...c, expr: e.target.value })} placeholder="amount > 100" /></div>
      </div>
    );
  }

  if (step.kind === "split") {
    const into = (c.into || []) as { name: string; expr: string }[];
    const setInto = (n: typeof into) => onChange({ ...c, into: n });
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Row>
          <div style={{ flex: 1 }}><span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Source column</span><Input value={c.column || ""} onChange={(e) => onChange({ ...c, column: e.target.value })} placeholder="posted_at" style={{ marginTop: 6 }} /></div>
        </Row>
        <ToggleRow on={!!c.drop_source} onClick={() => onChange({ ...c, drop_source: !c.drop_source })} title="Drop the source column" />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 28px", gap: 8, fontSize: 12, fontWeight: 500, color: "var(--text-3)" }}><span>New column</span><span>Expression</span><span /></div>
        {into.map((p, i) => (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr 28px", gap: 8, alignItems: "center" }}>
            <Input value={p.name} onChange={(e) => { const n = [...into]; n[i] = { ...p, name: e.target.value }; setInto(n); }} placeholder="posted_date" />
            <Input value={p.expr} onChange={(e) => { const n = [...into]; n[i] = { ...p, expr: e.target.value }; setInto(n); }} placeholder="CAST(posted_at AS DATE)" />
            <RemoveBtn onClick={() => setInto(into.filter((_, j) => j !== i))} />
          </div>
        ))}
        <Button variant="ghost" icon="add" height={28} padX={8} onClick={() => setInto([...into, { name: "", expr: "" }])} style={{ alignSelf: "flex-start", color: "var(--accent)", fontSize: 12 }}>Add output column</Button>
      </div>
    );
  }

  if (step.kind === "sql") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", gap: 8, fontSize: 12, color: "var(--text-3)" }}><Icon name="info" style={{ fontSize: 15 }} />Reference the previous step as <code>&#123;&#123; input &#125;&#125;</code>. Runs in DuckDB per batch.</div>
        <CodeEditor code={c.sql || ""} onChange={(v) => onChange({ sql: v })} lang="sql" />
      </div>
    );
  }

  return <PythonStep code={c.code || ""} onChange={(code) => onChange({ code })} columns={inputColumns} />;
}

function PythonStep({ code, onChange, columns }: { code: string; onChange: (code: string) => void; columns: string[] }) {
  const [guide, setGuide] = useState(false);
  const starter = (defaultConfig("python") as { code: string }).code;

  function use(example: string) {
    if (code.trim() && code.trim() !== starter.trim() && !confirm("Replace the current code with this example?")) return;
    onChange(example);
    setGuide(false);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", gap: 8, fontSize: 12, color: "var(--text-3)", alignItems: "center" }}>
        <Icon name="info" style={{ fontSize: 15 }} />
        <span style={{ flex: 1 }}>Define <code>def transform(df)</code>: receives and returns a pandas DataFrame. <code>pd</code> and <code>np</code> are ready to use.</span>
        <Button height={26} padX={8} icon="menu_book" style={{ fontSize: 12 }} onClick={() => setGuide(true)}>Guide &amp; examples</Button>
      </div>
      <CodeEditor code={code} onChange={onChange} lang="py" suggest columns={columns} />
      <div style={{ fontSize: 11, color: "var(--text-3)" }}>
        Suggestions appear after <code>df.</code>, <code>df["</code>, <code>.str.</code>, <code>.dt.</code>, <code>pd.</code>, <code>np.</code> · Ctrl+Space anywhere
        {columns.length > 0 && <> · columns: {columns.join(", ")}</>}
      </div>
      {guide && <PythonGuide onClose={() => setGuide(false)} onUse={use} columns={columns} />}
    </div>
  );
}
