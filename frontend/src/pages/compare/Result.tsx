import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { compareApi, type CompareExportParams, type CompareOut, type CompareRowsOut, type CompareSet, type CompareSummary } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { fmtEta, fmtN, relativeTime, statusBadge } from "../../lib/format";
import { Icon } from "../../ui/Icon";
import { Button } from "../../ui/Button";
import { useCan } from "../../lib/auth";
import { Card, CardHeader, CardBody } from "../../ui/Card";
import { StatusBadge } from "../../ui/Badge";
import { ProgressBar } from "../../ui/ProgressBar";
import { SegmentedControl } from "../../ui/SegmentedControl";

const ACTIVE = new Set(["queued", "running"]);
export const SET_LABEL: Record<CompareSet, string> = {
  missing: "Missing in target", extra: "Extra in target", mismatched: "Value differences",
  dup_source: "Duplicates in source", dup_target: "Duplicates in target",
};
const SET_HELP: Record<CompareSet, string> = {
  missing: "In the source but not in the target.",
  extra: "In the target but not in the source.",
  mismatched: "Same key on both sides, but at least one value differs. Differing cells show source → target.",
  dup_source: "Rows whose key (or whole row) occurs more than once in the source; _count is how often.",
  dup_target: "Rows whose key (or whole row) occurs more than once in the target; _count is how often.",
};
const ORDER: CompareSet[] = ["missing", "extra", "mismatched", "dup_source", "dup_target"];
const PAGE = 100;

export function CompareResult({ id, onChanged }: { id: string; onChanged: () => void }) {
  const can = useCan();
  const nav = useNavigate();
  const toast = useToast();
  const [c, setC] = useState<CompareOut | null>(null);
  const [tab, setTab] = useState<CompareSet>("missing");
  const wasActive = useRef(false);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    setC(null);
    wasActive.current = false;
    const load = () => compareApi.get(id).then((x) => {
      if (stop) return;
      setC(x);
      if (ACTIVE.has(x.status)) { wasActive.current = true; timer = setTimeout(load, 1000); }
      else {
        if (wasActive.current) onChanged();
        const sets = x.summary?.sets || {};
        setTab(ORDER.find((s) => (sets[s] || 0) > 0) || ORDER.find((s) => s in sets) || "missing");
      }
    }).catch((e) => !stop && toast("err", "Could not load the comparison", e instanceof Error ? e.message : undefined));
    load();
    return () => { stop = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (!c) return <div style={{ padding: 24, color: "var(--text-3)" }}>Loading…</div>;
  const s = c.summary;
  const active = ACTIVE.has(c.status);

  async function again() {
    if (!c?.spec) return;
    try { const n = await compareApi.create(c.spec); onChanged(); nav(`/compare/${n.id}`); }
    catch (e) { toast("err", "Could not start the comparison", e instanceof Error ? e.message : undefined); }
  }
  async function cancel() {
    try { setC(await compareApi.cancel(c!.id)); } catch (e) { toast("err", "Could not cancel", e instanceof Error ? e.message : undefined); }
  }
  async function remove() {
    if (!confirm("Delete this comparison and its results?")) return;
    try { await compareApi.remove(c!.id); onChanged(); nav("/compare"); }
    catch (e) { toast("err", "Could not delete", e instanceof Error ? e.message : undefined); }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
      <Card>
        <div style={{ display: "flex", gap: 12, alignItems: "flex-start", padding: "16px 18px", flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 240 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Icon name="difference" style={{ fontSize: 17, color: "var(--text-3)" }} />
              <span style={{ fontFamily: "'Geist Mono',monospace", fontWeight: 500, fontSize: 14 }}>{c.name}</span>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 4 }}>
              {c.source_label} → {c.target_label} · {c.finished_at ? `finished ${relativeTime(c.finished_at)}` : `started ${relativeTime(c.created_at)}`}
              {s?.seconds != null ? ` · took ${fmtEta(s.seconds)}` : ""}
              {c.job_id && <> · verifies <a href={`#/jobs/${c.job_id}`}>job {c.job_id.slice(0, 8)}</a></>}
            </div>
          </div>
          <StatusBadge b={statusBadge(c.status)} />
          {can.edit && active && <Button variant="danger" icon="stop_circle" onClick={cancel}>Cancel</Button>}
          {can.edit && !active && c.spec && <Button icon="edit" onClick={() => nav("/compare", { state: { fromSpec: c.spec } })}>Edit</Button>}
          {can.edit && !active && c.spec && <Button variant="primary" icon="replay" onClick={again}>Run again</Button>}
          {can.edit && !active && <Button variant="ghost" icon="delete" onClick={remove}>Delete</Button>}
        </div>
        {active && (
          <div style={{ padding: "0 18px 16px", display: "flex", flexDirection: "column", gap: 8 }}>
            <ProgressBar pct={100} color="var(--accent)" striped height={6} />
            <div style={{ fontSize: 12, color: "var(--text-2)" }}>
              {c.phase || "Queued"} · source {fmtN(c.rows_source)} rows · target {fmtN(c.rows_target)} rows read
            </div>
          </div>
        )}
        {c.error && (
          <div style={{ margin: "0 18px 16px", display: "flex", gap: 8, padding: "10px 12px", borderRadius: 6, background: "var(--err-soft)", fontSize: 12 }}>
            <Icon name="error" style={{ fontSize: 16, color: "var(--err)" }} /><span>{c.error}</span>
          </div>
        )}
        {c.status === "stalled" && (
          <div style={{ margin: "0 18px 16px", fontSize: 12, color: "var(--err)" }}>No progress for over 90 seconds: the worker probably restarted. Cancel it and run it again.</div>
        )}
      </Card>

      {c.status === "succeeded" && s && (
        <>
          <Verdict s={s} />
          <Tiles s={s} onPick={setTab} />
          <Details s={s} />
          {can.edit && <SetBrowser id={c.id} s={s} tab={tab} setTab={setTab} />}
          {can.edit && <Exporter id={c.id} s={s} />}
        </>
      )}
    </div>
  );
}

function Verdict({ s }: { s: CompareSummary }) {
  const ok = s.identical && !s.dup_source_rows && !s.dup_target_rows;
  const parts = [s.missing && `${fmtN(s.missing)} missing in the target`, s.extra && `${fmtN(s.extra)} extra in the target`,
    s.mismatched && `${fmtN(s.mismatched)} with different values`, (s.dup_source_rows || s.dup_target_rows) && "duplicates found"].filter(Boolean);
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "12px 16px", borderRadius: 8, background: ok ? "var(--ok-soft)" : "var(--warn-soft)", border: `1px solid ${ok ? "var(--ok)" : "var(--warn)"}` }}>
      <Icon name={ok ? "check_circle" : "difference"} style={{ color: ok ? "var(--ok)" : "var(--warn)" }} />
      <div>
        <div style={{ fontWeight: 600 }}>{ok ? "Source and target match" : `Differences: ${parts.join(", ")}`}</div>
        <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 2 }}>
          {s.mode === "key" ? <>Matched by <b style={{ fontFamily: "'Geist Mono',monospace" }}>{s.keys.join(", ")}</b> ({s.key_note})</> : "Whole rows compared (no key)"}
          {" · "}{s.columns.length} column{s.columns.length === 1 ? "" : "s"} compared
          {s.options?.ignore_nulls && s.mode === "key" ? " · nulls ignored" : ""}{s.options?.trim_text ? " · text trimmed" : ""}{s.options?.case_insensitive ? " · case ignored" : ""}
        </div>
      </div>
    </div>
  );
}

function Tiles({ s, onPick }: { s: CompareSummary; onPick: (t: CompareSet) => void }) {
  const tile = (label: string, value: number, sub: string, tone?: string, set?: CompareSet) => (
    <button key={label} onClick={() => set && onPick(set)} disabled={!set} className={set ? "hover-surface2" : undefined}
      style={{ background: "var(--surface)", padding: "12px 14px", border: "none", textAlign: "left", cursor: set ? "pointer" : "default" }}>
      <div style={{ fontSize: 12, color: "var(--text-3)" }}>{label}</div>
      <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 20, marginTop: 4, color: tone }}>{fmtN(value)}</div>
      <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 2 }}>{sub}</div>
    </button>
  );
  const bad = (n: number) => (n ? "var(--warn)" : "var(--ok)");
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 1, background: "var(--border)", border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
      {tile("Source rows", s.rows_source, "read")}
      {tile("Target rows", s.rows_target, "read")}
      {tile("Matched", s.matched, s.mode === "key" ? "same key, same values" : "identical rows", "var(--ok)")}
      {tile("Missing in target", s.missing, "only in the source", bad(s.missing), "missing")}
      {tile("Extra in target", s.extra, "only in the target", bad(s.extra), "extra")}
      {s.mode === "key" && tile("Value differences", s.mismatched, "same key, other values", bad(s.mismatched), "mismatched")}
      {"dup_source" in s.sets && tile("Duplicates", s.dup_source_rows + s.dup_target_rows, `${fmtN(s.dup_source_rows)} source · ${fmtN(s.dup_target_rows)} target rows`,
        bad(s.dup_source_rows + s.dup_target_rows), s.dup_source_rows ? "dup_source" : "dup_target")}
    </div>
  );
}

function Details({ s }: { s: CompareSummary }) {
  const [open, setOpen] = useState(false);
  const mism = Object.entries(s.column_mismatches).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...mism.map(([, n]) => n));
  return (
    <Card>
      <CardHeader icon="analytics" title="By column" right={<Button height={26} padX={8} style={{ fontSize: 12 }} icon={open ? "expand_less" : "expand_more"} onClick={() => setOpen(!open)}>{open ? "Hide details" : "Details"}</Button>} />
      <CardBody>
        {mism.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {mism.map(([name, n]) => (
              <div key={name} style={{ display: "grid", gridTemplateColumns: "minmax(120px,220px) 1fr 80px", gap: 10, alignItems: "center", fontSize: 12 }}>
                <span style={{ fontFamily: "'Geist Mono',monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
                <ProgressBar pct={(n / max) * 100} color="var(--warn)" height={6} />
                <span style={{ fontFamily: "'Geist Mono',monospace", textAlign: "right" }}>{fmtN(n)}</span>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ fontSize: 12, color: "var(--text-3)" }}>{s.mode === "key" ? "No column has differing values." : "Per-column differences need a key column."}</div>
        )}
        {s.notes.map((n, i) => <div key={i} style={{ display: "flex", gap: 8, fontSize: 12, color: "var(--text-2)" }}><Icon name="info" style={{ fontSize: 15, color: "var(--accent)" }} />{n}</div>)}
        {open && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ border: "1px solid var(--border)", borderRadius: 6, overflow: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead><tr>{["Column", "Source column", "Source type", "Target type", "Compared as"].map((h) => <th key={h} style={{ textAlign: "left", padding: "6px 10px", borderBottom: "1px solid var(--border)", color: "var(--text-3)", fontWeight: 500 }}>{h}</th>)}</tr></thead>
                <tbody>
                  {s.columns.map((col) => (
                    <tr key={col.name}>
                      {[col.name + (s.keys.includes(col.name) ? " 🔑" : ""), col.source, col.source_type, col.target_type, col.compared_as].map((v, i) => (
                        <td key={i} style={{ padding: "5px 10px", borderBottom: "1px solid var(--border)", fontFamily: "'Geist Mono',monospace" }}>{v}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {(s.unpaired_source.length > 0 || s.unpaired_target.length > 0) && (
              <div style={{ fontSize: 12, color: "var(--text-3)" }}>
                Not compared · source: {s.unpaired_source.join(", ") || "—"} · target: {s.unpaired_target.join(", ") || "—"}
              </div>
            )}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function fmtCell(v: unknown): ReactNode {
  if (v === null || v === undefined) return <span style={{ color: "var(--text-3)" }}>∅</span>;
  return String(v);
}

function SetBrowser({ id, s, tab, setTab }: { id: string; s: CompareSummary; tab: CompareSet; setTab: (t: CompareSet) => void }) {
  const toast = useToast();
  const [page, setPage] = useState(0);
  const [data, setData] = useState<CompareRowsOut | null>(null);
  const [loading, setLoading] = useState(false);
  const sets = ORDER.filter((x) => x in s.sets);

  useEffect(() => { setPage(0); }, [tab]);
  useEffect(() => {
    if (!(tab in s.sets)) return;
    setLoading(true);
    compareApi.rows(id, tab, page * PAGE, PAGE).then(setData)
      .catch((e) => toast("err", "Could not load rows", e instanceof Error ? e.message : undefined))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, tab, page]);

  const total = s.sets[tab] || 0;
  return (
    <Card>
      <div style={{ display: "flex", gap: 4, padding: "0 12px", borderBottom: "1px solid var(--border)", overflowX: "auto" }}>
        {sets.map((x) => {
          const on = x === tab, n = s.sets[x] || 0;
          return (
            <button key={x} onClick={() => setTab(x)} style={{ height: 40, display: "flex", alignItems: "center", gap: 6, padding: "0 10px", border: "none", borderBottom: `2px solid ${on ? "var(--accent)" : "transparent"}`, marginBottom: -1, background: "transparent", color: on ? "var(--text)" : "var(--text-2)", fontWeight: 500, cursor: "pointer", whiteSpace: "nowrap" }}>
              {SET_LABEL[x]}
              <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, padding: "0 6px", borderRadius: 9, background: n ? "var(--warn-soft)" : "var(--surface-3)", color: n ? "var(--warn)" : "var(--text-3)" }}>{fmtN(n)}</span>
            </button>
          );
        })}
      </div>
      <div style={{ padding: "10px 14px", display: "flex", alignItems: "center", gap: 10, fontSize: 12, color: "var(--text-3)", flexWrap: "wrap" }}>
        <span style={{ flex: 1 }}>{SET_HELP[tab]}</span>
        {loading && <Icon name="progress_activity" spin style={{ fontSize: 15 }} />}
        {total > PAGE && (
          <>
            <span>{fmtN(page * PAGE + 1)}–{fmtN(Math.min(total, (page + 1) * PAGE))} of {fmtN(total)}</span>
            <Button height={26} padX={6} icon="chevron_left" disabled={page === 0} onClick={() => setPage(page - 1)} />
            <Button height={26} padX={6} icon="chevron_right" disabled={(page + 1) * PAGE >= total} onClick={() => setPage(page + 1)} />
          </>
        )}
      </div>
      {total === 0 ? (
        <div style={{ padding: "0 14px 16px", fontSize: 12, color: "var(--ok)", display: "flex", gap: 6, alignItems: "center" }}><Icon name="check" style={{ fontSize: 16 }} />Nothing here.</div>
      ) : data && data.set === tab ? (
        <div style={{ overflow: "auto", maxHeight: 520, borderTop: "1px solid var(--border)" }}>
          {tab === "mismatched" ? <MismatchTable data={data} keys={s.keys} /> : <PlainTable data={data} keys={s.keys} />}
        </div>
      ) : null}
    </Card>
  );
}

const th = { position: "sticky" as const, top: 0, background: "var(--surface-2)", borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", padding: "6px 10px", textAlign: "left" as const, whiteSpace: "nowrap" as const, fontFamily: "'Geist Mono',monospace", fontWeight: 500, fontSize: 12 };
const td = { borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", padding: "0 10px", height: 28, whiteSpace: "nowrap" as const, fontFamily: "'Geist Mono',monospace", fontSize: 12 };

function PlainTable({ data, keys }: { data: CompareRowsOut; keys: string[] }) {
  return (
    <table style={{ borderCollapse: "separate", borderSpacing: 0, minWidth: "100%" }}>
      <thead><tr>{data.columns.map((c) => <th key={c} style={th}>{keys.includes(c) && <Icon name="key" style={{ fontSize: 12, marginRight: 4, color: "var(--accent)" }} />}{c}</th>)}</tr></thead>
      <tbody>{data.rows.map((r, i) => <tr key={i}>{data.columns.map((c) => <td key={c} style={td}>{fmtCell(r[c])}</td>)}</tr>)}</tbody>
    </table>
  );
}

function MismatchTable({ data, keys }: { data: CompareRowsOut; keys: string[] }) {
  const valueCols = data.columns.filter((c) => c.endsWith("__source")).map((c) => c.slice(0, -"__source".length));
  return (
    <table style={{ borderCollapse: "separate", borderSpacing: 0, minWidth: "100%" }}>
      <thead><tr>
        {keys.map((k) => <th key={k} style={th}><Icon name="key" style={{ fontSize: 12, marginRight: 4, color: "var(--accent)" }} />{k}</th>)}
        {valueCols.map((c) => <th key={c} style={th}>{c} <span style={{ color: "var(--text-3)", fontWeight: 400 }}>source → target</span></th>)}
      </tr></thead>
      <tbody>
        {data.rows.map((r, i) => {
          const diff = new Set(String(r._diff_columns || "").split(", ").filter(Boolean));
          return (
            <tr key={i}>
              {keys.map((k) => <td key={k} style={td}>{fmtCell(r[k])}</td>)}
              {valueCols.map((c) => diff.has(c) ? (
                <td key={c} style={{ ...td, background: "var(--err-soft)" }}>
                  <span>{fmtCell(r[`${c}__source`])}</span>
                  <Icon name="arrow_forward" style={{ fontSize: 12, margin: "0 6px", color: "var(--err)", verticalAlign: "middle" }} />
                  <b style={{ color: "var(--err)" }}>{fmtCell(r[`${c}__target`])}</b>
                </td>
              ) : (
                <td key={c} style={{ ...td, color: "var(--text-3)" }}>{fmtCell(r[`${c}__source`])}</td>
              ))}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Exporter({ id, s }: { id: string; s: CompareSummary }) {
  const toast = useToast();
  const available = ORDER.filter((x) => x in s.sets);
  const nonEmpty = available.filter((x) => (s.sets[x] || 0) > 0);
  const [sets, setSets] = useState<CompareSet[]>(nonEmpty.length ? nonEmpty : available.slice(0, 1));
  const [colMode, setColMode] = useState<"all" | "keys" | "pick">("all");
  const [picked, setPicked] = useState<string[]>(s.columns.map((c) => c.name));
  const [format, setFormat] = useState<CompareExportParams["format"]>("csv");
  const [rows, setRows] = useState<"all" | "sample">("all");
  const [busy, setBusy] = useState(false);
  const sample = s.options?.sample_rows ?? 100;
  const nonKey = s.columns.map((c) => c.name).filter((n) => !s.keys.includes(n));

  async function download() {
    setBusy(true);
    try {
      // key columns are added by the server for "pick" too; nothing picked means just the keys
      const columns = colMode !== "pick" ? colMode : picked.length ? picked : "keys";
      const { blob, filename } = await compareApi.download(id, { sets, columns, format, rows });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast("err", "Export failed", e instanceof Error ? e.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  const label = (t: string) => <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>{t}</span>;
  return (
    <Card>
      <CardHeader icon="download" title="Export" />
      <CardBody>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(240px,1fr))", gap: 16 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {label("Results")}
            {available.map((x) => (
              <label key={x} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
                <input type="checkbox" checked={sets.includes(x)} onChange={() => setSets(sets.includes(x) ? sets.filter((y) => y !== x) : ORDER.filter((y) => y === x || sets.includes(y)))} />
                {SET_LABEL[x]} <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)" }}>{fmtN(s.sets[x] || 0)}</span>
              </label>
            ))}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {label("Columns")}
            <SegmentedControl small value={colMode} onChange={setColMode}
              options={[{ id: "all", label: "All compared" }, ...(s.keys.length ? [{ id: "keys" as const, label: "Keys only" }] : []), { id: "pick", label: "Choose…" }]} />
            {colMode === "pick" && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {(s.keys.length ? nonKey : s.columns.map((c) => c.name)).map((n) => {
                  const on = picked.includes(n);
                  return (
                    <button key={n} onClick={() => setPicked(on ? picked.filter((p) => p !== n) : [...picked, n])} style={{ height: 24, padding: "0 8px", borderRadius: 5, border: `1px solid ${on ? "var(--accent)" : "var(--border-2)"}`, background: on ? "var(--accent-soft)" : "var(--surface)", color: on ? "var(--accent)" : "var(--text-2)", fontFamily: "'Geist Mono',monospace", fontSize: 11, cursor: "pointer" }}>{n}</button>
                  );
                })}
              </div>
            )}
            <span style={{ fontSize: 11, color: "var(--text-3)" }}>{s.keys.length ? "Key columns are always included, so every row can be found again." : ""}</span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {label("Format")}
            <SegmentedControl small value={format} onChange={setFormat} options={[{ id: "csv", label: "CSV" }, { id: "xlsx", label: "Excel" }, { id: "parquet", label: "Parquet" }, { id: "json", label: "JSON" }]} />
            {label("Rows")}
            <SegmentedControl small value={rows} onChange={setRows} options={[{ id: "all", label: "Everything" }, { id: "sample", label: `Sample (${fmtN(sample)})` }]} />
            <span style={{ fontSize: 11, color: "var(--text-3)" }}>
              {format === "xlsx" ? "One sheet per result; Excel stops at 1,048,575 rows." : sets.length > 1 ? "Several results download as one .zip." : format === "json" ? "One JSON object per line." : ""}
            </span>
          </div>
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <Button variant="primary" icon="download" disabled={busy || !sets.length} onClick={download}>{busy ? "Preparing…" : "Download"}</Button>
        </div>
      </CardBody>
    </Card>
  );
}
