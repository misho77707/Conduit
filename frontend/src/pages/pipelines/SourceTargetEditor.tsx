import { useEffect, useId, useState, type ReactNode } from "react";
import { connectionsApi, databasesOnly, type CombinedIn, type ConnectionOut, type SourceIn, type TargetIn } from "../../lib/api";
import { emptyRemote, RemotePicker } from "../remote/RemotePicker";
import { Field } from "../../ui/Field";
import { Input, Select, TextArea } from "../../ui/Input";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { Icon } from "../../ui/Icon";
import { newCombined } from "./combined";

type Mode = "append" | "replace" | "upsert";

type Kind = "database" | "file" | "remote" | "combined";

/**
 * A table / query, a server file or a remote file. `combined` (pipelines only) adds a fourth kind, several inputs
 * joined by SQL, and renders its editor. `asInput`: this is one input of a combined source.
 */
export function SourceEditor({ value, onChange, conns, combined, asInput }: {
  value: SourceIn; onChange: (v: SourceIn) => void; conns: ConnectionOut[];
  combined?: (c: CombinedIn, set: (c: CombinedIn) => void) => ReactNode; asInput?: boolean;
}) {
  const isFile = !!value.file;
  const kind: Kind = value.combined ? "combined" : value.remote ? "remote" : isFile ? "file" : "database";
  const view = value.query != null ? "query" : "table";
  const conn = conns.find((c) => c.id === value.connection_id);
  const [tables, setTables] = useState<string[]>([]);
  const listId = useId();

  useEffect(() => {  // table names for the Table field's suggestions
    setTables([]);
    if (value.connection_id) connectionsApi.tables(value.connection_id).then((ts) => setTables(ts.map((t) => t.name))).catch(() => {});
  }, [value.connection_id]);

  function setKind(k: Kind) {
    if (k === kind) return;
    if (kind === "combined" && !confirm("Switch away from the combined source? Its inputs and SQL are discarded.")) return;
    if (k === "combined") onChange({ combined: newCombined(value, conns), where: "" });
    else if (k === "file") onChange({ file: { path: "", header: true, encoding: "utf-8", skip_rows: 0, null_values: [""] }, where: "" });
    else if (k === "remote") onChange({ remote: emptyRemote(conns.find((c) => c.type === "s3" || c.type === "sftp")?.id || null), where: "" });
    else onChange({ connection_id: databasesOnly(conns)[0]?.id || null, table: "", where: "" });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <SegmentedControl small value={kind} onChange={setKind} options={[
        { id: "database", label: "Database" }, { id: "file", label: "Server file" }, { id: "remote", label: "Remote file" },
        ...(combined ? [{ id: "combined" as const, label: "Combined" }] : []),
      ]} />
      {kind === "combined" && combined ? (
        combined(value.combined!, (c) => onChange({ combined: c, where: "" }))
      ) : kind === "remote" ? (
        <RemotePicker value={value.remote || null} onChange={(r) => onChange({ remote: r, where: "" })} conns={conns} needsFormat={!!value.remote?.format} asInput={asInput} />
      ) : kind === "file" ? (
        <Field label="File path" hint="Path on the Conduit host, inside an allowed data folder.">
          <Input value={value.file?.path || ""} onChange={(e) => onChange({ ...value, file: { ...(value.file as any), path: e.target.value } })} />
        </Field>
      ) : (
        <>
          <Field label="Connection">
            <Select value={value.connection_id || ""} onChange={(e) => onChange({ connection_id: e.target.value, table: "", query: null, where: value.where })}>
              <option value="">Choose…</option>
              {databasesOnly(conns).filter((c) => c.type !== "mongodb" || view === "table").map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
          <SegmentedControl small options={[{ id: "table", label: "Table" }, { id: "query", label: "SQL query" }].filter((o) => o.id === "table" || conn?.type !== "mongodb")} value={view} onChange={(v) => onChange(v === "query" ? { connection_id: value.connection_id, query: "", where: "" } : { connection_id: value.connection_id, table: "", where: "" })} />
          {view === "table" ? (
            <Field label="Table">
              <Input list={listId} value={value.table || ""} onChange={(e) => onChange({ ...value, table: e.target.value, query: null })} placeholder={tables[0] || "public.orders"} />
              <datalist id={listId}>{tables.map((t) => <option key={t} value={t} />)}</datalist>
            </Field>
          ) : (
            <Field label="Query"><TextArea rows={3} value={value.query || ""} onChange={(e) => onChange({ ...value, query: e.target.value, table: null })} placeholder="SELECT * FROM public.orders" /></Field>
          )}
          <Field label="Extra WHERE filter · optional">
            <Input value={value.where || ""} onChange={(e) => onChange({ ...value, where: e.target.value })} placeholder="status <> 'test'" />
          </Field>
        </>
      )}
    </div>
  );
}

export function TargetEditor({ value, onChange, conns }: { value: TargetIn; onChange: (v: TargetIn) => void; conns: ConnectionOut[] }) {
  const [keyDraft, setKeyDraft] = useState("");
  const mongo = conns.find((c) => c.id === value.connection_id)?.type === "mongodb";

  function addKey() {
    const k = keyDraft.trim();
    if (k && !value.keys.includes(k)) onChange({ ...value, keys: [...value.keys, k] });
    setKeyDraft("");
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Field label="Connection">
        <Select value={value.connection_id || ""} onChange={(e) => onChange({ ...value, connection_id: e.target.value })}>
          <option value="">Choose…</option>
          {databasesOnly(conns).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </Select>
      </Field>
      <Field label={mongo ? "Collection" : "Table"} hint={mongo ? undefined : "Include a schema, e.g. public.orders_clean"}>
        <Input value={value.table} onChange={(e) => onChange({ ...value, table: e.target.value })} />
      </Field>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Load mode</span>
        <SegmentedControl small options={[{ id: "append", label: "Append" }, { id: "replace", label: "Replace" }, { id: "upsert", label: "Upsert" }]} value={value.mode} onChange={(m: Mode) => onChange({ ...value, mode: m })} />
      </div>
      {value.mode === "upsert" && (
        <Field label="Key column(s)">
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            {value.keys.map((k) => (
              <span key={k} style={{ height: 24, padding: "0 8px", borderRadius: 5, border: "1px solid var(--accent)", background: "var(--accent-soft)", color: "var(--accent)", fontFamily: "'Geist Mono',monospace", fontSize: 11, display: "inline-flex", alignItems: "center", gap: 4 }}>
                <Icon name="key" style={{ fontSize: 13 }} />{k}
                <button onClick={() => onChange({ ...value, keys: value.keys.filter((x) => x !== k) })} style={{ border: "none", background: "none", color: "inherit", cursor: "pointer", padding: 0, display: "flex" }}><Icon name="close" style={{ fontSize: 13 }} /></button>
              </span>
            ))}
            <input value={keyDraft} onChange={(e) => setKeyDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addKey())} placeholder="column name, Enter" style={{ height: 24, width: 140, padding: "0 6px", borderRadius: 5, border: "1px solid var(--border-2)", background: "var(--surface)", fontSize: 11, fontFamily: "'Geist Mono',monospace" }} />
          </div>
        </Field>
      )}
    </div>
  );
}
