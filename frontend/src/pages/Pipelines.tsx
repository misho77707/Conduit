import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { connectionsApi, pipelinesApi, type ConnectionOut, type PipelineOut } from "../lib/api";
import { useToast } from "../lib/toast";
import { cronLabel, relativeTime, statusBadge, untilTime } from "../lib/format";
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { RowMenu } from "../ui/RowMenu";
import { StatusBadge } from "../ui/Badge";
import { NewPipelineModal } from "./pipelines/NewPipelineModal";

function sourceLabel(p: PipelineOut, conns: ConnectionOut[]): { icon: string; text: string } {
  if (p.source.file) return { icon: "description", text: p.source.file.path.split("/").pop() || p.source.file.path || "file" };
  const conn = conns.find((c) => c.id === p.source.connection_id);
  const what = p.source.table || (p.source.query ? "query" : "?");
  return { icon: "database", text: `${conn?.name || "?"} · ${what}` };
}

export function Pipelines() {
  const nav = useNavigate();
  const toast = useToast();
  const [pipes, setPipes] = useState<PipelineOut[] | null>(null);
  const [conns, setConns] = useState<ConnectionOut[]>([]);
  const [addOpen, setAddOpen] = useState(false);

  const load = () => pipelinesApi.list().then(setPipes).catch(() => toast("err", "Could not load pipelines"));

  useEffect(() => {
    load();
    connectionsApi.list().then(setConns).catch(() => {});
  }, []);

  async function runNow(p: PipelineOut) {
    try {
      const job = await pipelinesApi.run(p.id);
      nav(`/jobs/${job.id}`);
    } catch (e) {
      toast("err", `Could not run ${p.name}`, e instanceof Error ? e.message : undefined);
    }
  }

  async function remove(p: PipelineOut) {
    if (!confirm(`Delete pipeline "${p.name}"?`)) return;
    try { await pipelinesApi.remove(p.id); load(); }
    catch (e) { toast("err", "Could not delete the pipeline", e instanceof Error ? e.message : undefined); }
  }

  async function toggleEnabled(p: PipelineOut) {
    try { await pipelinesApi.update(p.id, { enabled: !p.enabled }); load(); }
    catch (e) { toast("err", "Could not update the schedule", e instanceof Error ? e.message : undefined); }
  }

  if (!pipes) return null;

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1440 }}>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Pipelines</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>Saved, reusable loads: read → transform → load.</p>
        </div>
        <Button variant="primary" icon="add" onClick={() => setAddOpen(true)}>New pipeline</Button>
      </div>

      {pipes.length === 0 ? (
        <div style={{ background: "var(--surface)", border: "1px dashed var(--border-2)", borderRadius: 10, padding: "64px 24px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", gap: 14 }}>
          <span style={{ width: 48, height: 48, borderRadius: 12, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Icon name="account_tree" style={{ fontSize: 26 }} />
          </span>
          <div style={{ fontSize: 16, fontWeight: 600 }}>No pipelines yet</div>
          <p style={{ margin: 0, maxWidth: 420, color: "var(--text-2)" }}>Save a reusable read → transform → load recipe with visual steps, SQL or Python.</p>
          <Button variant="primary" icon="add" onClick={() => setAddOpen(true)}>New pipeline</Button>
        </div>
      ) : (
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}>
          <div style={{ minWidth: 980 }}>
            <div style={{ display: "grid", gridTemplateColumns: "minmax(220px,1.4fr) 170px minmax(220px,1.4fr) 60px 150px 110px 70px", gap: 12, padding: "0 16px", height: 36, alignItems: "center", borderBottom: "1px solid var(--border)", fontSize: 12, fontWeight: 500, color: "var(--text-3)" }}>
              <span>Pipeline</span><span>Source</span><span>Target</span><span>Steps</span><span>Last run</span><span>Schedule</span><span />
            </div>
            {pipes.map((p) => {
              const src = sourceLabel(p, conns);
              const targetConn = conns.find((c) => c.id === p.target.connection_id);
              const b = p.last_run ? statusBadge(p.last_run.status) : null;
              return (
                <div key={p.id} className="hover-surface2" style={{ display: "grid", gridTemplateColumns: "minmax(220px,1.4fr) 170px minmax(220px,1.4fr) 60px 150px 110px 70px", gap: 12, padding: "0 16px", minHeight: 54, alignItems: "center", borderBottom: "1px solid var(--border)", position: "relative" }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontFamily: "'Geist Mono',monospace", fontWeight: 500 }}>{p.name}</div>
                    <div style={{ fontSize: 12, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.description || " "}</div>
                  </div>
                  <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}><Icon name={src.icon} style={{ fontSize: 16, color: "var(--text-3)" }} />{src.text}</span>
                  <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{targetConn?.name || "?"} · {p.target.table}</span>
                  <span style={{ fontFamily: "'Geist Mono',monospace", color: "var(--text-2)" }}>{p.steps.length}</span>
                  <div style={{ display: "flex", flexDirection: "column", gap: 2, alignItems: "flex-start" }}>
                    {b ? <StatusBadge b={b} size="sm" /> : <span style={{ fontSize: 12, color: "var(--text-3)" }}>never run</span>}
                    {p.last_run?.finished_at && <span style={{ fontSize: 11, color: "var(--text-3)" }}>{relativeTime(p.last_run.finished_at)}</span>}
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    <span style={{ fontSize: 12, color: p.schedule && !p.enabled ? "var(--text-3)" : "var(--text-2)" }}>{p.schedule ? cronLabel(p.schedule) : "manual"}</span>
                    {p.schedule && (
                      <span style={{ fontSize: 11, color: "var(--text-3)" }}>
                        {p.enabled ? (p.output_columns ? untilTime(p.next_run_at) : "no preview yet") : "paused"}
                      </span>
                    )}
                  </div>
                  <div style={{ display: "flex", gap: 4, justifyContent: "flex-end", position: "relative" }}>
                    <Button height={28} padX={10} onClick={() => nav(`/pipelines/${p.id}`)} style={{ fontSize: 12 }}>Open</Button>
                    <RowMenu width={170} items={[
                      { icon: "play_arrow", label: "Run now", onClick: () => runNow(p), disabled: !p.output_columns },
                      ...(p.schedule ? [{ icon: p.enabled ? "pause" : "play_arrow", label: p.enabled ? "Pause schedule" : "Resume schedule", onClick: () => toggleEnabled(p) }] : []),
                      { icon: "delete", label: "Delete", tone: "var(--err)", onClick: () => remove(p) },
                    ]} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {addOpen && <NewPipelineModal conns={conns} onClose={() => setAddOpen(false)} onCreated={(id) => nav(`/pipelines/${id}`)} />}
    </div>
  );
}
