import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { jobsApi, type JobSnapshot } from "../lib/api";
import { fmtEta, fmtN, prePhase, relativeTime, statusBadge } from "../lib/format";
import { Button } from "../ui/Button";
import { StatusBadge } from "../ui/Badge";
import { ProgressBar } from "../ui/ProgressBar";
import { Icon } from "../ui/Icon";
import { JobDetail } from "./jobs/JobDetail";
import { useCan } from "../lib/auth";

const ACTIVE = new Set(["queued", "planning", "running"]);

export function Jobs() {
  const can = useCan();
  const nav = useNavigate();
  const { id } = useParams();
  const [jobs, setJobs] = useState<JobSnapshot[] | null>(null);

  const load = () => jobsApi.list().then(setJobs);

  useEffect(() => {
    load();
    const iv = setInterval(load, 4000);
    return () => clearInterval(iv);
  }, []);

  useEffect(() => {
    if (!id && jobs && jobs.length > 0) nav(`/jobs/${jobs[0].id}`, { replace: true });
  }, [id, jobs]);

  if (!jobs) return null;

  const running = jobs.filter((j) => ACTIVE.has(j.status) && j.status !== "queued");
  const queued = jobs.filter((j) => j.status === "queued");
  const recent = jobs.filter((j) => !ACTIVE.has(j.status));
  const groups: { label: string; items: JobSnapshot[] }[] = [
    { label: `Running · ${running.length}`, items: running },
    ...(queued.length ? [{ label: `Queued · ${queued.length}`, items: queued }] : []),
    { label: "Most recent", items: recent },
  ];

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1440 }}>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Jobs</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>What&rsquo;s running, what&rsquo;s next, and the last result. Conduit doesn&rsquo;t keep a long history.</p>
        </div>
        {can.edit && <Button variant="primary" icon="add" onClick={() => nav("/upload")}>New load</Button>}
      </div>

      {jobs.length === 0 ? (
        <div style={{ background: "var(--surface)", border: "1px dashed var(--border-2)", borderRadius: 10, padding: "64px 24px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", gap: 14 }}>
          <span style={{ width: 48, height: 48, borderRadius: 12, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Icon name="bolt" style={{ fontSize: 26 }} />
          </span>
          <div style={{ fontSize: 16, fontWeight: 600 }}>No jobs yet</div>
          {can.edit && <Button variant="primary" icon="add" onClick={() => nav("/upload")}>Start a load</Button>}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(260px,320px) minmax(0,1fr)", gap: 16, alignItems: "start" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {groups.map((g) => (
              <div key={g.label} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.04em", padding: "0 2px" }}>{g.label}</div>
                {g.items.length === 0 && <div style={{ fontSize: 12, color: "var(--text-3)", padding: "0 2px" }}>None</div>}
                {g.items.map((j) => {
                  const badge = statusBadge(j.status);
                  const selected = j.id === id;
                  const pct = j.progress != null ? j.progress * 100 : 0;
                  const active = ACTIVE.has(j.status);
                  return (
                    <button
                      key={j.id}
                      onClick={() => nav(`/jobs/${j.id}`)}
                      className="hover-surface2"
                      style={{ display: "flex", flexDirection: "column", gap: 8, padding: 12, borderRadius: 8, border: `1px solid ${selected ? "var(--accent)" : "var(--border)"}`, boxShadow: selected ? "0 0 0 3px var(--accent-soft)" : "none", background: "var(--surface)", cursor: "pointer", textAlign: "left" }}
                    >
                      <div style={{ display: "flex", gap: 8, alignItems: "center", width: "100%" }}>
                        <span style={{ flex: 1, minWidth: 0, fontFamily: "'Geist Mono',monospace", fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.name}</span>
                        <StatusBadge b={badge} size="sm" />
                      </div>
                      {active && <ProgressBar pct={pct} color="var(--warn)" height={4} />}
                      <div style={{ fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "100%" }}>
                        {active && prePhase(j.phase) ? prePhase(j.phase) : active ? `${pct.toFixed(1)}% · ${fmtN(j.rows_written)} rows` : j.finished_at ? `finished · ${relativeTime(j.finished_at)}` : relativeTime(j.created_at)}
                        {j.status === "running" && j.eta_s != null ? ` · ~${fmtEta(j.eta_s)} left` : ""}
                        {j.started_by ? ` · by ${j.started_by}` : ""}
                      </div>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          {id ? <JobDetail jobId={id} onChanged={load} /> : <div />}
        </div>
      )}
    </div>
  );
}
