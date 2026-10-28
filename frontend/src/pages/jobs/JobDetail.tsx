import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { jobsApi, type JobDetail as JobDetailT, type JobPlan } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { fmtEta, fmtN, prePhase, statusBadge } from "../../lib/format";
import { Icon } from "../../ui/Icon";
import { Button } from "../../ui/Button";
import { StatusBadge } from "../../ui/Badge";
import { ProgressBar } from "../../ui/ProgressBar";
import { useCan } from "../../lib/auth";
import { MovementSummary } from "./RowMovement";

const ACTIVE = new Set(["queued", "planning", "running", "stalled"]);

function fmtClock(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString([], { hour12: false });
}
function elapsedLabel(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt) return "—";
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  const s = Math.max(0, (end - new Date(startedAt).getTime()) / 1000);
  return fmtEta(s);
}

export function JobDetail({ jobId, onChanged }: { jobId: string; onChanged: () => void }) {
  const toast = useToast();
  const nav = useNavigate();
  const can = useCan();
  const [job, setJob] = useState<JobDetailT | null>(null);
  const [cancelAsk, setCancelAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setJob(null);
    setCancelAsk(false);
    const es = new EventSource(jobsApi.eventsUrl(jobId));
    es.addEventListener("job", (e) => setJob(JSON.parse((e as MessageEvent).data)));
    es.addEventListener("done", () => { es.close(); onChanged(); });
    es.onerror = () => { /* browser auto-retries; nothing to do */ };
    return () => es.close();
  }, [jobId]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [job?.log.length]);

  if (!job) return <div style={{ padding: 24, color: "var(--text-3)" }}>Loading…</div>;

  const active = ACTIVE.has(job.status);
  const combined = !!job.spec?.source?.combined;
  const notes = ((job.plan as JobPlan | null)?.notes || []).filter((n) => typeof n === "string");
  const pct = job.progress != null ? job.progress * 100 : 0;
  const badge = statusBadge(job.status);
  const barColor = job.status === "succeeded" ? "var(--ok)" : job.status === "cancelled" ? "var(--idle)" : job.status === "failed" || job.status === "stalled" ? "var(--err)" : "var(--accent)";

  async function cancel() {
    setBusy(true);
    try { await jobsApi.cancel(job!.id); toast("warn", "Cancel requested"); } finally { setBusy(false); setCancelAsk(false); }
  }
  async function retry() {
    setBusy(true);
    try { await jobsApi.retry(job!.id); toast("info", "Retry queued"); } finally { setBusy(false); }
  }
  async function remove() {
    if (!confirm("Remove this job from the list?")) return;
    await jobsApi.remove(job!.id);
    onChanged();
  }
  async function downloadRejects() {
    try {
      const blob = await jobsApi.downloadRejects(job!.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `${job!.id}.rejects.csv`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast("err", "Could not download rejects", e instanceof Error ? e.message : undefined);
    }
  }

  return (
    <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, minWidth: 0 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "flex-start", padding: "16px 18px", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 260 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Icon name={combined ? "join_inner" : job.kind === "file_load" ? "description" : "database"} style={{ fontSize: 17, color: "var(--text-3)" }} />
            <span style={{ fontFamily: "'Geist Mono',monospace", fontWeight: 500, fontSize: 14 }}>{job.name}</span>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 4, fontFamily: "'Geist Mono',monospace", overflowWrap: "anywhere" }}>
            {job.source_label} → {job.target_label}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 2, fontFamily: "'Geist Mono',monospace" }}>
            job {job.id.slice(0, 8)} · {job.started_at ? `started ${fmtClock(job.started_at)}` : "not started"} · {job.workers} worker{job.workers === 1 ? "" : "s"}
            {" · "}started by {job.started_by || "a deleted user"}
          </div>
        </div>
        <StatusBadge b={badge} />
        {can.edit && active && job.status !== "queued" && (
          <div style={{ position: "relative" }}>
            <Button variant="danger" icon="stop_circle" disabled={busy} onClick={() => setCancelAsk(true)}>Cancel</Button>
            {cancelAsk && (
              <div style={{ position: "absolute", right: 0, top: 38, width: 300, padding: 14, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, boxShadow: "var(--shadow-2)", zIndex: 5, display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ fontWeight: 600 }}>Cancel this job?</div>
                <div style={{ fontSize: 12, color: "var(--text-2)" }}>{fmtN(job.rows_written)} rows already committed stay in the target. Open splits roll back to their last committed batch.</div>
                <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                  <Button height={28} padX={10} onClick={() => setCancelAsk(false)} style={{ fontSize: 12 }}>Keep running</Button>
                  <Button variant="danger-solid" height={28} padX={10} onClick={cancel} style={{ fontSize: 12 }}>Cancel job</Button>
                </div>
              </div>
            )}
          </div>
        )}
        {can.edit && (job.status === "failed" || job.status === "cancelled" || job.status === "stalled") && (
          <Button variant="primary" icon="replay" disabled={busy} onClick={retry}>Retry</Button>
        )}
        {can.edit && job.kind !== "pipeline" && !active && (
          <Button icon="difference" onClick={() => nav("/compare", { state: { fromJobId: job.id } })} title="Compare this load's source with its target">Verify</Button>
        )}
        {can.edit && job.kind !== "pipeline" && (
          <Button icon="edit" onClick={() => nav("/upload", { state: { fromJobId: job.id } })} title="Open this load in Upload & Load with every setting filled in">Edit &amp; re-run</Button>
        )}
        {can.edit && !active && (
          <Button variant="ghost" icon="delete" onClick={remove}>Remove</Button>
        )}
      </div>

      <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 18 }}>
        {job.error && (
          <div style={{ display: "flex", gap: 8, padding: "10px 12px", borderRadius: 6, background: "var(--err-soft)", color: "var(--err)", fontSize: 12 }}>
            <Icon name="error" style={{ fontSize: 16 }} /><span style={{ color: "var(--text)" }}>{job.error}</span>
          </div>
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
            <span style={{ fontSize: 28, fontWeight: 600, fontFamily: "'Geist Mono',monospace", letterSpacing: "-0.02em" }}>{job.progress != null ? `${pct.toFixed(1)}%` : "—"}</span>
            <span style={{ color: "var(--text-2)", fontFamily: "'Geist Mono',monospace" }}>
              {fmtN(job.rows_written)} {job.rows_total != null ? `/ ${job.rows_total_exact ? "" : "~"}${fmtN(job.rows_total)}` : ""} rows
            </span>
            <div style={{ flex: 1 }} />
            <span style={{ fontSize: 12, color: "var(--text-3)" }}>{job.splits_done} / {job.splits_total} splits done</span>
          </div>
          <ProgressBar pct={pct} color={barColor} striped={job.status === "running"} />
          {combined && active && job.phase && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-2)" }}>
              <Icon name="hourglass_top" style={{ fontSize: 15, color: "var(--text-3)" }} />
              <span style={{ fontFamily: "'Geist Mono',monospace" }}>{job.phase}</span>
              {prePhase(job.phase) && <span style={{ color: "var(--text-3)" }}>· rows load after this</span>}
            </div>
          )}
        </div>

        {combined && notes.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span style={{ fontWeight: 600 }}>Combined source</span>
            {notes.map((n) => (
              <div key={n} style={{ display: "flex", gap: 6, fontSize: 12, color: "var(--text-2)" }}><Icon name="info" style={{ fontSize: 15, color: "var(--text-3)", flexShrink: 0 }} />{n}</div>
            ))}
          </div>
        )}

        {/* a viewer's plan has notes / warnings only, so this shows nothing to them */}
        <MovementSummary plan={job.plan as Partial<JobPlan> | null} />

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 1, background: "var(--border)", border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
          <Tile label="Rows written" value={fmtN(job.rows_written)} sub={job.rows_total != null ? `of ${job.rows_total_exact ? "" : "~"}${fmtN(job.rows_total)}` : "no estimate"} />
          <Tile label="Throughput" value={job.rate_rows_s ? `${fmtN(job.rate_rows_s)} rows/s` : "—"} sub={active ? "live" : "final"} />
          <Tile label="Elapsed" value={elapsedLabel(job.started_at, job.finished_at)} sub={job.status === "running" && job.eta_s != null ? `~${fmtEta(job.eta_s)} remaining` : job.status} />
          <div style={{ background: "var(--surface)", padding: "12px 14px" }}>
            <div style={{ fontSize: 12, color: "var(--text-3)" }}>Rejected rows</div>
            <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 18, marginTop: 4, color: job.rows_rejected ? "var(--warn)" : "var(--ok)", display: "flex", gap: 6, alignItems: "center" }}>
              <Icon name={job.rows_rejected ? "warning" : "check"} style={{ fontSize: 17 }} />{fmtN(job.rows_rejected)}
            </div>
            {job.has_rejects && can.edit ? (
              <a href="#" onClick={(e) => { e.preventDefault(); downloadRejects(); }} style={{ fontSize: 12, marginTop: 2, display: "inline-flex", gap: 4, alignItems: "center" }}>
                <Icon name="download" style={{ fontSize: 14 }} />rejects.csv
              </a>
            ) : (
              <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 2 }}>{job.has_rejects ? "reject file (admins)" : "no reject file"}</div>
            )}
          </div>
        </div>

        {job.splits.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontWeight: 600 }}>Splits</span>
              <span style={{ fontSize: 12, color: "var(--text-3)" }}>each split reads its own range independently</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(210px,1fr))", gap: 8 }}>
              {job.splits.map((s) => {
                const b = statusBadge(s.status);
                const w = s.est_rows ? Math.min(100, (s.rows_written / s.est_rows) * 100) : s.status === "done" ? 100 : 0;
                const barC = s.status === "done" ? "var(--ok)" : s.status === "failed" ? "var(--err)" : "var(--accent)";
                return (
                  <div key={s.idx} style={{ border: `1px solid ${s.status === "failed" ? "var(--err)" : "var(--border)"}`, borderRadius: 7, padding: "10px 12px", display: "flex", flexDirection: "column", gap: 7 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)" }}>{s.idx + 1}</span>
                      <span style={{ flex: 1, minWidth: 0, fontFamily: "'Geist Mono',monospace", fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.label}</span>
                    </div>
                    <ProgressBar pct={w} color={barC} height={4} />
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <StatusBadge b={b} size="sm" />
                      <span style={{ flex: 1 }} />
                      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)" }}>
                        {fmtN(s.rows_written)}{s.est_rows ? ` / ~${fmtN(s.est_rows)}` : ""}
                      </span>
                    </div>
                    {s.error && <div style={{ fontSize: 11, color: "var(--err)" }}>{s.error.slice(0, 120)}</div>}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", borderBottom: "1px solid var(--border)", background: "var(--surface-2)" }}>
            <Icon name="terminal" style={{ fontSize: 16, color: "var(--text-3)" }} />
            <span style={{ fontWeight: 500, flex: 1 }}>Live log</span>
            {active && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-3)" }}>
                <span style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--ok)", animation: "pulse 1.6s infinite" }} />following
              </span>
            )}
          </div>
          <div ref={logRef} style={{ background: "var(--code-bg)", padding: "10px 0", fontFamily: "'Geist Mono',monospace", fontSize: 12, lineHeight: "20px", height: 260, overflow: "auto" }}>
            {job.log.map((l, i) => (
              <div key={i} style={{ display: "flex", gap: 12, padding: "0 12px", whiteSpace: "nowrap" }}>
                <span style={{ color: "var(--text-3)" }}>{new Date(l.t * 1000).toLocaleTimeString([], { hour12: false })}</span>
                <span style={{ width: 40, color: l.level === "error" ? "var(--err)" : l.level === "warn" ? "var(--warn)" : "var(--text-3)", fontWeight: 500 }}>{l.level.toUpperCase()}</span>
                <span style={{ color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis" }}>{l.msg}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Tile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div style={{ background: "var(--surface)", padding: "12px 14px" }}>
      <div style={{ fontSize: 12, color: "var(--text-3)" }}>{label}</div>
      <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 18, marginTop: 4 }}>{value}</div>
      <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 2 }}>{sub}</div>
    </div>
  );
}
