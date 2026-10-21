import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { compareApi, type CompareOut } from "../lib/api";
import { fmtN, relativeTime, statusBadge } from "../lib/format";
import { Button } from "../ui/Button";
import { StatusBadge } from "../ui/Badge";
import { CompareSetup } from "./compare/Setup";
import { CompareResult } from "./compare/Result";
import { useCan } from "../lib/auth";
import { Icon } from "../ui/Icon";

function line(c: CompareOut): string {
  const s = c.summary;
  if (c.status === "running" || c.status === "queued") return `${c.phase || "Queued"} · ${fmtN(c.rows_source + c.rows_target)} rows read`;
  if (c.status === "succeeded" && s) {
    if (s.identical && !s.dup_source_rows && !s.dup_target_rows) return `identical · ${fmtN(s.rows_source)} rows`;
    return [s.missing && `${fmtN(s.missing)} missing`, s.extra && `${fmtN(s.extra)} extra`, s.mismatched && `${fmtN(s.mismatched)} different`,
      (s.dup_source_rows || s.dup_target_rows) && "duplicates"].filter(Boolean).join(" · ");
  }
  return c.error ? c.error.slice(0, 60) : c.status;
}

export function Compare() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const can = useCan();
  const [list, setList] = useState<CompareOut[] | null>(null);

  const load = () => compareApi.list().then(setList).catch(() => setList([]));
  useEffect(() => { load(); }, [id]);
  useEffect(() => {  // keep the list's progress lines fresh while something runs
    if (!list?.some((c) => c.status === "running" || c.status === "queued")) return;
    const t = setTimeout(load, 2000);
    return () => clearTimeout(t);
  }, [list]);

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1440 }}>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Compare</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>Check that a target holds exactly what the source has: missing rows, extra rows, changed values and duplicates.</p>
        </div>
        {id && can.edit && <Button variant="primary" icon="add" onClick={() => nav("/compare")}>New comparison</Button>}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(240px,300px) minmax(0,1fr)", gap: 16, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.04em", padding: "0 2px" }}>Recent</div>
          {list && list.length === 0 && <div style={{ fontSize: 12, color: "var(--text-3)", padding: "0 2px" }}>No comparisons yet. Only the latest few are kept.</div>}
          {!id && can.edit && list && list.length > 0 && (
            <div style={{ padding: 12, borderRadius: 8, border: "1px solid var(--accent)", boxShadow: "0 0 0 3px var(--accent-soft)", background: "var(--surface)", fontSize: 12, fontWeight: 500 }}>New comparison</div>
          )}
          {list?.map((c) => {
            const on = c.id === id;
            return (
              <button key={c.id} onClick={() => nav(`/compare/${c.id}`)} className="hover-surface2"
                style={{ display: "flex", flexDirection: "column", gap: 6, padding: 12, borderRadius: 8, border: `1px solid ${on ? "var(--accent)" : "var(--border)"}`, boxShadow: on ? "0 0 0 3px var(--accent-soft)" : "none", background: "var(--surface)", cursor: "pointer", textAlign: "left" }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", width: "100%" }}>
                  <span style={{ flex: 1, minWidth: 0, fontFamily: "'Geist Mono',monospace", fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
                  <StatusBadge b={statusBadge(c.status)} size="sm" />
                </div>
                <div style={{ fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{line(c)} · {relativeTime(c.finished_at || c.created_at)}</div>
              </button>
            );
          })}
        </div>
        {id ? <CompareResult id={id} onChanged={load} />
          : can.edit ? <CompareSetup onStarted={(c) => { load(); nav(`/compare/${c.id}`); }} />
          : (
            <div style={{ background: "var(--surface)", border: "1px dashed var(--border-2)", borderRadius: 10, padding: "48px 24px", display: "flex", flexDirection: "column", alignItems: "center", gap: 10, textAlign: "center", color: "var(--text-2)" }}>
              <Icon name="difference" style={{ fontSize: 26, color: "var(--text-3)" }} />
              {list && list.length ? "Pick a comparison on the left to see its counts." : "No comparisons yet."}
            </div>
          )}
      </div>
    </div>
  );
}
