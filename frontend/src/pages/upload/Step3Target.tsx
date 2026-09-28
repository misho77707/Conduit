import { useEffect, useState } from "react";
import { connectionsApi, jobsApi, type TableOut } from "../../lib/api";
import { dbTile, statusBadge } from "../../lib/format";
import { highlight } from "../../lib/highlight";
import { Icon } from "../../ui/Icon";
import { Card } from "../../ui/Card";
import { Input, Select } from "../../ui/Input";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ToggleRow } from "../../ui/Toggle";
import { StatusBadge } from "../../ui/Badge";
import type { ColumnMapUI, Mode, WizardState } from "./types";
import type { WizardCtx } from "./Wizard";
import { buildJobIn } from "./jobBuilder";

interface Props { s: WizardState; set: (p: Partial<WizardState>) => void; ctx: WizardCtx; }

const MODE_DESC: Record<Mode, string> = {
  append: "Insert all rows. Existing rows are untouched.",
  replace: "Truncate the target, then insert.",
  upsert: "Insert new rows and update rows that match the key.",
};

export function Step3Target({ s, set, ctx }: Props) {
  const [targetTables, setTargetTables] = useState<TableOut[] | null>(null);
  const mongo = ctx.conns.find((c) => c.id === s.targetConnId)?.type === "mongodb";

  useEffect(() => {
    if (s.targetConnId) connectionsApi.tables(s.targetConnId).then(setTargetTables).catch(() => setTargetTables(null));
  }, [s.targetConnId]);

  useEffect(() => {
    if (!s.targetConnId || !s.targetTable.trim()) return;
    const t = setTimeout(() => {
      set({ planLoading: true, planError: null });
      jobsApi.plan(buildJobIn(s), 0)
        .then((plan) => set({ plan, planLoading: false }))
        .catch((e) => set({ planError: e instanceof Error ? e.message : "Could not build a plan", planLoading: false, plan: null }));
    }, 500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.targetConnId, s.targetTable, s.tableMode, s.mode, JSON.stringify(s.keys), JSON.stringify(s.columns)]);

  const included = s.columns.filter((c) => c.include);
  const updCol = (i: number, patch: Partial<ColumnMapUI>) => set({ columns: s.columns.map((c, j) => (j === i ? { ...c, ...patch } : c)) });

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(420px,1fr))", gap: 16, alignItems: "start" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Card>
          <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border)", fontWeight: 600 }}>Target connection</div>
          {ctx.conns.map((c) => {
            const tile = dbTile(c.type, ctx.dark);
            const on = c.id === s.targetConnId;
            return (
              <button key={c.id} onClick={() => set({ targetConnId: c.id, targetTable: "" })} className="hover-surface2" style={{ width: "100%", display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", border: "none", borderBottom: "1px solid var(--border)", background: on ? "var(--accent-soft)" : "transparent", cursor: "pointer", textAlign: "left" }}>
                <span style={{ width: 14, height: 14, borderRadius: "50%", border: `1.5px solid ${on ? "var(--accent)" : "var(--border-2)"}`, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>{on && <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--accent)" }} />}</span>
                <span style={{ width: 24, height: 24, borderRadius: 5, background: tile.tbg, color: tile.tfg, fontFamily: "'Geist Mono',monospace", fontSize: 10, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>{ctx.dbTypes[c.type]?.abbr}</span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: "block", fontWeight: 500 }}>{c.name}</span>
                  <span style={{ display: "block", fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.host}</span>
                </span>
                <StatusBadge b={statusBadge(c.status)} size="sm" />
              </button>
            );
          })}
        </Card>

        {s.targetConnId && (
          <Card>
            <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 14 }}>
              <div style={{ fontWeight: 600 }}>Destination</div>
              {!mongo ? (
                <>
                  <SegmentedControl options={[{ id: "new", label: "Create new table" }, { id: "existing", label: "Existing table" }]} value={s.tableMode} onChange={(id) => set({ tableMode: id, targetTable: "" })} />
                  {s.tableMode === "new" ? (
                    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>New table name <span style={{ fontWeight: 400, color: "var(--text-3)" }}>· include a schema, e.g. public.orders</span></span>
                      <Input value={s.targetTable} onChange={(e) => set({ targetTable: e.target.value })} />
                    </label>
                  ) : (
                    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Existing table</span>
                      <Select value={s.targetTable} onChange={(e) => set({ targetTable: e.target.value })}>
                        <option value="">Choose…</option>
                        {(targetTables || []).map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
                      </Select>
                    </label>
                  )}
                </>
              ) : (
                <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Collection</span>
                  <Input value={s.targetTable} onChange={(e) => set({ targetTable: e.target.value })} />
                </label>
              )}

              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Load mode</span>
                <SegmentedControl options={[{ id: "append", label: "Append" }, { id: "replace", label: "Replace" }, { id: "upsert", label: "Upsert" }]} value={s.mode} onChange={(id) => set({ mode: id })} />
                <span style={{ fontSize: 12, color: "var(--text-3)" }}>{MODE_DESC[s.mode]}</span>
                {s.mode === "upsert" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: 10, border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface-2)" }}>
                    <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Key column(s)</span>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {included.map((c) => {
                        const on = s.keys.includes(c.name);
                        return (
                          <button key={c.name} onClick={() => set({ keys: on ? s.keys.filter((k) => k !== c.name) : [...s.keys, c.name] })} style={{ height: 24, padding: "0 8px", borderRadius: 5, border: `1px solid ${on ? "var(--accent)" : "var(--border-2)"}`, background: on ? "var(--accent-soft)" : "var(--surface)", color: on ? "var(--accent)" : "var(--text-2)", fontFamily: "'Geist Mono',monospace", fontSize: 11, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 4 }}>
                            <Icon name={on ? "key" : "add"} style={{ fontSize: 13 }} />{c.name}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              <ToggleRow on={s.rejectToFile} onClick={() => set({ rejectToFile: !s.rejectToFile })} title="Reject bad rows to a file" sub={s.rejectToFile ? "Bad rows are written to a reject CSV; the job keeps going." : "The job stops at the first invalid row."} />

              {included.length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: 8, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Columns</span>
                  {s.columns.map((c, i) => (
                    <div key={c.source} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 8, alignItems: "center" }}>
                      <Input value={c.name} onChange={(e) => updCol(i, { name: e.target.value })} />
                      <Select value={c.type} onChange={(e) => updCol(i, { type: e.target.value })}>
                        <option value="(auto)">(auto) · {c.nativeType}</option>
                      </Select>
                      <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
                        <input type="checkbox" checked={c.include} onChange={() => updCol(i, { include: !c.include })} />include
                      </label>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Card>
        )}
      </div>

      <Card style={{ position: "sticky", top: 16, overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px" }}>
          <div style={{ fontWeight: 600, flex: 1 }}>{mongo ? "Target preview" : "Generated DDL"}</div>
          {s.planLoading && <Icon name="progress_activity" spin style={{ fontSize: 16, color: "var(--text-3)" }} />}
        </div>
        {s.planError && <div style={{ padding: "0 14px 14px", color: "var(--err)", fontSize: 12 }}>{s.planError}</div>}
        {s.plan && (
          <div style={{ background: "var(--code-bg)", borderTop: "1px solid var(--border)", padding: "12px 0", fontFamily: "'Geist Mono',monospace", fontSize: 12, lineHeight: "20px", overflow: "auto", maxHeight: 420 }}>
            {(s.plan.ddl.length ? highlight(s.plan.ddl.join("\n\n"), "sql") : highlight(mongo ? "-- rows are inserted as flat documents; field names match your column names" : "-- no DDL: writing into the existing table", "sql")).map((ln) => (
              <div key={ln.n} style={{ display: "flex", whiteSpace: "pre", minHeight: 20 }}>
                <span style={{ width: 40, flexShrink: 0, textAlign: "right", paddingRight: 14, color: "var(--text-3)", userSelect: "none" }}>{ln.n}</span>
                <span>{ln.segs.map((sg, i) => <span key={i} style={{ color: sg.c }}>{sg.t}</span>)}</span>
              </div>
            ))}
          </div>
        )}
        {s.plan && (
          <div style={{ padding: 14, borderTop: "1px solid var(--border)", fontSize: 12, color: "var(--text-2)", display: "flex", flexDirection: "column", gap: 4 }}>
            <div>{s.plan.rows_total != null ? `${s.plan.rows_total_exact ? "" : "~"}${s.plan.rows_total.toLocaleString()} rows` : "row count unknown"} · {s.plan.target_exists ? "existing table" : "will be created"}</div>
            {s.plan.notes.map((n, i) => <div key={i} style={{ color: "var(--text-3)" }}>{n}</div>)}
            {s.plan.warnings.map((w, i) => <div key={i} style={{ color: "var(--warn)" }}>{w}</div>)}
          </div>
        )}
      </Card>
    </div>
  );
}
