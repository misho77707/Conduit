import { useEffect, useState } from "react";
import { jobsApi, remoteApi, type RemoteMatchOut } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { fileFormatLabel, statusBadge } from "../../lib/format";
import { Icon } from "../../ui/Icon";
import { Card } from "../../ui/Card";
import { Button } from "../../ui/Button";
import { sourceMode, type WizardState } from "./types";
import { remoteLabel } from "../remote/RemotePicker";
import type { WizardCtx } from "./Wizard";
import { buildJobIn } from "./jobBuilder";
import { MovementEditor } from "../jobs/RowMovement";
import type { TransferMethodInfo } from "../../lib/api";

interface Props { s: WizardState; set: (p: Partial<WizardState>) => void; ctx: WizardCtx; onCreated: (id: string) => void; }

export function Step4Review({ s, set, ctx, onCreated }: Props) {
  const toast = useToast();
  const [running, setRunning] = useState(false);

  // what a run would load from a remote newest / all source into this target (new / changed / already loaded)
  const [targetMatch, setTargetMatch] = useState<RemoteMatchOut | null>(null);
  useEffect(() => {
    if (sourceMode(s) !== "remote" || !s.remote || s.remote.spec.match === "exact" || !s.targetConnId) return;
    remoteApi.match(s.remote.spec, { connection_id: s.targetConnId, table: s.targetTable, mode: s.mode }).then(setTargetMatch).catch(() => setTargetMatch(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // the last transfer-method info a plan returned: kept while a plan with the pull chosen fails, so the choice
  // stays on screen and can be switched back
  const [method, setMethod] = useState<TransferMethodInfo | null>(null);
  useEffect(() => {
    set({ planLoading: true, planError: null });
    jobsApi.plan(buildJobIn(s), 2000)
      .then((plan) => { set({ plan, planLoading: false }); setMethod(plan.transfer_method ?? null); })
      .catch((e) => set({ plan: null, planError: e instanceof Error ? e.message : "Could not build a plan", planLoading: false }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.movement]);

  const sourceConn = ctx.conns.find((c) => c.id === s.sourceConnId);
  const targetConn = ctx.conns.find((c) => c.id === s.targetConnId);
  const included = s.columns.filter((c) => c.include);
  const plan = s.plan;

  const mode = sourceMode(s);
  const remote = mode === "remote" ? s.remote : null;
  const sourceLabel = remote ? (remote.spec.match === "exact" ? remote.display : remoteLabel(remote.spec, ctx.conns))
    : mode === "file" ? s.uploadedName || s.filePath || "" : s.sourceView === "query" ? "custom query" : s.sourceTable || "";
  const fileSub = s.fileInspect ? `${fileFormatLabel(s.fileInspect.format, s.fileInspect.compression)} · ${s.fileInspect.columns.length} columns` : "";
  const summary = [
    { k: "Source", v: sourceLabel, s: remote ? `${remote.spec.match === "exact" ? "one file" : remote.spec.match === "newest" ? "newest match" : "all matches"} · ${fileSub}` : mode === "file" ? fileSub : sourceConn?.name || "" },
    { k: "Schema", v: `${included.length} of ${s.columns.length || included.length} columns`, s: plan ? `${plan.target_columns.length} target columns` : "" },
    { k: "Target", v: targetConn?.name || "", s: s.targetTable + (plan?.target_exists ? " · existing" : s.tableMode === "new" && s.ddlText ? " · created with your edited DDL" : " · will be created") },
    { k: "Load mode", v: s.mode[0].toUpperCase() + s.mode.slice(1), s: s.mode === "upsert" ? `key: ${s.keys.join(", ") || "—"}` : `chunk ${s.chunk.toLocaleString()} rows` },
    { k: "Bad rows", v: s.rejectToFile ? "Reject to file" : "Stop the job", s: s.rejectToFile ? "written to a reject CSV" : "fails on first invalid row" },
    { k: "Rows", v: plan?.rows_total != null ? `${plan.rows_total_exact ? "" : "~"}${plan.rows_total.toLocaleString()}` : "unknown", s: plan?.split_column ? `split on ${plan.split_column} · ${plan.workers} workers` : "single stream" },
  ];

  const checks: { icon: string; c: string; t: string; s: string }[] = [];
  if (remote) {
    const m = targetMatch;
    if (remote.spec.match === "exact" || !m) {
      checks.push({ icon: "check_circle", c: "var(--ok)", t: "Remote file ready", s: `${remote.display}${remote.sampled ? " · previewed from a sample" : ""}` });
    } else {
      const loading = m.chosen.length;
      const skipped = m.skip ? m.loaded_count : 0;
      checks.push(loading
        ? { icon: "check_circle", c: "var(--ok)", t: `${loading} file${loading === 1 ? "" : "s"} to load`, s: [m.new_count && `${m.new_count} new`, m.changed_count && `${m.changed_count} changed`, skipped && `${skipped} already loaded, skipped`].filter(Boolean).join(" · ") || `${m.total} matching` }
        : { icon: "info", c: "var(--warn)", t: "Nothing new to load right now", s: m.notes[0] || "Every matching file is already loaded into this target: a run now finishes with 0 rows." });
      const changedLoading = m.files.filter((f) => f.status === "changed" && m.chosen.includes(f.path)).length;
      if (changedLoading && s.mode === "append") {
        checks.push({ icon: "warning", c: "var(--warn)", t: `${changedLoading} changed file${changedLoading === 1 ? "" : "s"} load again`, s: "Overwritten in place since they were loaded: in append mode their rows are added again (upsert avoids duplicates)." });
      }
    }
  } else if (mode === "file") {
    checks.push({ icon: "check_circle", c: "var(--ok)", t: "File ready", s: s.fileInspect ? `${fileFormatLabel(s.fileInspect.format, s.fileInspect.compression)} · ${s.fileInspect.row_estimate != null ? s.fileInspect.row_estimate.toLocaleString() : "unknown"} rows` : "" });
  } else if (sourceConn) {
    const b = statusBadge(sourceConn.status);
    checks.push({ icon: b.icon, c: b.fg, t: sourceConn.status === "err" ? "Source unreachable" : "Source reachable", s: `${sourceConn.name} · ${sourceConn.note || sourceConn.host}` });
  }
  if (targetConn) {
    const b = statusBadge(targetConn.status);
    checks.push({ icon: b.icon, c: b.fg, t: targetConn.status === "err" ? "Target unreachable" : "Target reachable", s: `${targetConn.name} · ${targetConn.note || targetConn.host}` });
  }
  if (plan?.split_column) {
    checks.push({ icon: "check_circle", c: "var(--ok)", t: "Splitting in parallel", s: `${plan.split_column} · ${plan.workers} workers` });
  } else if (mode === "database") {
    checks.push({ icon: "info", c: "var(--accent)", t: "Single stream", s: "No split column chosen: the source is read in one piece." });
  }
  checks.push({ icon: "info", c: "var(--accent)", t: "No global snapshot", s: "Each split reads its own snapshot. Rows changing mid-run can be missed or read twice at split borders." });

  const blocked = sourceConn?.status === "err" || targetConn?.status === "err";

  async function dryRun() {
    try {
      const p = await jobsApi.plan(buildJobIn(s), 2000);
      set({ plan: p });
      if (p.sample) toast("ok", "Dry run passed", `${p.sample.rows.toLocaleString()} rows sampled · ${p.sample.rejected} would be rejected · ~${p.sample.rows_per_s.toLocaleString()} rows/s`);
      else toast("ok", "Plan is valid", "No sample was read (nothing to preview).");
    } catch (e) {
      toast("err", "Dry run failed", e instanceof Error ? e.message : undefined);
    }
  }

  async function runLoad() {
    if (blocked) return toast("err", "Cannot start load", "Fix the unreachable connection first.");
    setRunning(true);
    try {
      const job = await jobsApi.create(buildJobIn(s));
      onCreated(job.id);
    } catch (e) {
      toast("err", "Could not start the job", e instanceof Error ? e.message : undefined);
      setRunning(false);
    }
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(380px,1fr))", gap: 16, alignItems: "start" }}>
      <Card>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: "1px solid var(--border)" }}>
          <Icon name="fact_check" style={{ color: "var(--accent)" }} />
          <div style={{ fontWeight: 600, flex: 1 }}>Load summary</div>
          {s.planLoading && <Icon name="progress_activity" spin style={{ fontSize: 16, color: "var(--text-3)" }} />}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))" }}>
          {summary.map((row) => (
            <div key={row.k} style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)" }}>
              <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.04em" }}>{row.k}</div>
              <div style={{ fontWeight: 500, marginTop: 4, overflowWrap: "anywhere" }}>{row.v}</div>
              <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)", marginTop: 3, wordBreak: "break-all" }}>{row.s}</div>
            </div>
          ))}
        </div>
        {plan?.notes.map((n, i) => (
          <div key={i} style={{ padding: "8px 16px", fontSize: 12, color: "var(--text-3)", borderBottom: "1px solid var(--border)" }}>{n}</div>
        ))}
        {plan?.warnings.map((w, i) => (
          <div key={i} style={{ padding: "8px 16px", fontSize: 12, color: "var(--warn)", borderBottom: "1px solid var(--border)" }}>{w}</div>
        ))}
        {s.planError && <div style={{ padding: "8px 16px", fontSize: 12, color: "var(--err)" }}>{s.planError}</div>}
        <div style={{ display: "flex", gap: 8, padding: "14px 16px", alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ flex: 1 }} />
          <Button icon="science" onClick={dryRun}>Dry run</Button>
          <Button variant="primary" icon="play_arrow" disabled={running || blocked} onClick={runLoad}>{running ? "Starting…" : "Run load"}</Button>
        </div>
      </Card>
      <Card>
        {(method || mode === "database" || ctx.dbTypes[targetConn?.type || ""]?.drivers.length) ? (
          <>
            <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)", fontWeight: 600 }}>How rows move</div>
            <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border)" }}>
              <MovementEditor value={s.movement} onChange={(movement) => set({ movement })} method={method} drivers={plan?.drivers}
                sourceConn={mode === "database" ? sourceConn : undefined} targetConn={targetConn} dbTypes={ctx.dbTypes} />
            </div>
          </>
        ) : null}
        <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)", fontWeight: 600 }}>Pre-flight checks</div>
        {checks.map((c, i) => (
          <div key={i} style={{ display: "flex", gap: 10, padding: "12px 16px", borderBottom: "1px solid var(--border)" }}>
            <Icon name={c.icon} style={{ color: c.c }} />
            <div>
              <div style={{ fontWeight: 500 }}>{c.t}</div>
              <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 2 }}>{c.s}</div>
            </div>
          </div>
        ))}
        {plan?.sample && (
          <div style={{ padding: "12px 16px", fontSize: 12, color: "var(--text-2)" }}>
            Sample: {plan.sample.rows.toLocaleString()} rows in {plan.sample.seconds}s (~{plan.sample.rows_per_s.toLocaleString()} rows/s){plan.sample.rejected ? `, ${plan.sample.rejected} would be rejected` : ""}.
          </div>
        )}
      </Card>
    </div>
  );
}
