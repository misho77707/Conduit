import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  connectionsApi, pipelinesApi, type ConnectionOut, type OptionsIn, type PipelineOut,
  type PipelinePreviewOut, type SourceIn, type StepIn, type StepKind, type TargetIn,
} from "../../lib/api";
import { useToast } from "../../lib/toast";
import { cronLabel, untilTime } from "../../lib/format";
import { Icon } from "../../ui/Icon";
import { Button, IconButton } from "../../ui/Button";
import { Input } from "../../ui/Input";
import { STEP_META, newStep, stepSummary } from "./types";
import { SourceEditor, TargetEditor } from "./SourceTargetEditor";
import { StepEditor } from "./StepEditor";
import { remoteLabel } from "../remote/RemotePicker";
import { ScheduleModal } from "./ScheduleModal";

interface Draft {
  name: string; description: string; source: SourceIn; target: TargetIn; options: OptionsIn; steps: StepIn[];
}

function draftOf(p: PipelineOut): Draft {
  return { name: p.name, description: p.description, source: p.source, target: p.target, options: p.options, steps: p.steps };
}

const STEP_KINDS: StepKind[] = ["rename", "cast", "filter", "derive", "split", "sql", "python"];

export function Builder() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const toast = useToast();
  const [conns, setConns] = useState<ConnectionOut[]>([]);
  const [pipeline, setPipeline] = useState<PipelineOut | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [selected, setSelected] = useState<string>("target");
  const [addMenu, setAddMenu] = useState(false);
  const [preview, setPreview] = useState<PipelinePreviewOut | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  // The step the last preview stopped at. Kept until a preview runs through that step again cleanly, so the
  // sidebar still flags it while an earlier step is selected (whose preview never reaches it).
  const [stepError, setStepError] = useState<{ id: string; message: string } | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const reqId = useRef(0);

  useEffect(() => { connectionsApi.list().then(setConns).catch(() => {}); }, []);

  useEffect(() => {
    if (!id) return;
    pipelinesApi.get(id).then((p) => {
      setPipeline(p);
      setDraft(draftOf(p));
      setSelected(p.steps.length ? p.steps[p.steps.length - 1].id : "source");
    }).catch((e) => toast("err", "Could not load the pipeline", e instanceof Error ? e.message : undefined));
  }, [id]);

  function uptoStepFor(sel: string, steps: StepIn[]): string | null {
    if (sel === "target") return null;
    if (sel === "source") return steps.length ? steps[0].id : null;
    return sel;
  }

  useEffect(() => {
    if (!draft || !id) return;
    const myReq = ++reqId.current;
    const t = setTimeout(async () => {
      if (!draft.name.trim() || !draft.target.connection_id || !draft.target.table.trim()) return;
      setPreviewLoading(true);
      setSaveError(null);
      setPreviewError(null);
      try {
        const saved = await pipelinesApi.update(id, {
          name: draft.name, description: draft.description, source: draft.source,
          target: draft.target, options: draft.options, steps: draft.steps,
        });
        if (myReq !== reqId.current) return;
        setPipeline(saved);
        try {
          const upto = uptoStepFor(selected, draft.steps);
          const p = await pipelinesApi.preview(id, 50, upto);
          if (myReq !== reqId.current) return;
          setPreview(p);
          if (p.full_chain) setPipeline((cur) => (cur ? { ...cur, output_columns: p.columns } : cur));
          const ids = draft.steps.map((s) => s.id);
          const ranThrough = upto === null ? ids.length - 1 : ids.indexOf(upto);
          setStepError((cur) => {
            if (p.error && p.error_step) return { id: p.error_step, message: p.error };
            return cur && ids.includes(cur.id) && ids.indexOf(cur.id) > ranThrough ? cur : null;
          });
        } catch (e) {
          if (myReq !== reqId.current) return;
          // keep the last table on screen (marked stale) rather than blanking the panel
          setPreviewError(e instanceof Error ? e.message : "Preview failed");
        }
      } catch (e) {
        if (myReq !== reqId.current) return;
        setSaveError(e instanceof Error ? e.message : "Could not save the pipeline");
      } finally {
        if (myReq === reqId.current) setPreviewLoading(false);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, 700);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, selected, id]);

  if (!draft || !pipeline) return null;

  const inputColumnsBefore = (sel: string): string[] => {
    // best-effort: columns known from the cached/last preview, else empty
    return preview?.columns.map((c) => c.name) || [];
  };

  function addStep(kind: StepKind) {
    const s = newStep(kind);
    setDraft((d) => (d ? { ...d, steps: [...d.steps, s] } : d));
    setSelected(s.id);
    setAddMenu(false);
  }
  function removeStep(stepId: string) {
    setDraft((d) => {
      if (!d) return d;
      const steps = d.steps.filter((s) => s.id !== stepId);
      if (selected === stepId) setSelected(steps.length ? steps[steps.length - 1].id : "source");
      return { ...d, steps };
    });
  }
  function updateStepConfig(stepId: string, config: Record<string, unknown>) {
    setDraft((d) => (d ? { ...d, steps: d.steps.map((s) => (s.id === stepId ? { ...s, config } : s)) } : d));
  }

  async function runNow() {
    setRunning(true);
    try {
      // Always force a fresh full-chain preview right before running, regardless of what's selected in the
      // sidebar: the sidebar's own auto-preview scopes to the selected step (upto_step), which only caches
      // the schema the backend needs for /run when the selection happens to be "Load" (upto_step omitted).
      // Since adding/selecting a step normally leaves selection on that step, not Load, relying on the
      // sidebar alone would leave /run permanently blocked after perfectly ordinary editing.
      const p = await pipelinesApi.preview(id!, 10, null);
      setPreview((cur) => (selected === "target" ? p : cur));
      if (p.error && p.error_step) {
        setStepError({ id: p.error_step, message: p.error });
        setSelected(p.error_step);
        toast("err", "Fix the highlighted step first", p.error);
        return;
      }
      setPipeline((cur) => (cur ? { ...cur, output_columns: p.columns } : cur));
      const job = await pipelinesApi.run(id!);
      nav(`/jobs/${job.id}`);
    } catch (e) {
      toast("err", "Could not run the pipeline", e instanceof Error ? e.message : undefined);
    } finally {
      setRunning(false);
    }
  }

  const selectedStep = draft.steps.find((s) => s.id === selected);
  const canRun = !running;
  const incomplete = !!previewError || !!preview?.error;
  const stepNo = (stepId: string | null) => draft.steps.findIndex((s) => s.id === stepId) + 1;
  const stepLabel = (stepId: string | null) => {
    const s = draft.steps.find((x) => x.id === stepId);
    return s ? STEP_META[s.kind].label : "step";
  };

  return (
    <div style={{ padding: "16px 20px 32px", display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <IconButton icon="arrow_back" onClick={() => nav("/pipelines")} style={{ border: "1px solid var(--border-2)" }} />
        <div style={{ flex: 1, minWidth: 200 }}>
          <Input mono value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} style={{ fontSize: 15, fontWeight: 600, height: 34, border: "1px solid transparent", background: "transparent", padding: "0 4px" }} />
          <div style={{ fontSize: 12, color: "var(--text-3)", padding: "0 4px" }}>
            {previewLoading ? "Saving…" : saveError ? <span style={{ color: "var(--err)" }}>{saveError}</span> : "Saved"}
          </div>
        </div>
        <Button
          icon="schedule" onClick={() => setScheduleOpen(true)}
          style={pipeline.schedule && pipeline.enabled ? { borderColor: "var(--accent)", color: "var(--accent)" } : pipeline.schedule ? { color: "var(--text-3)" } : undefined}
        >
          {!pipeline.schedule ? "Schedule" : pipeline.enabled ? `${cronLabel(pipeline.schedule)} · next ${untilTime(pipeline.next_run_at)}` : `${cronLabel(pipeline.schedule)} · paused`}
        </Button>
        <Button icon="visibility" onClick={() => setDraft({ ...draft })}>Refresh preview</Button>
        <Button variant="primary" icon="play_arrow" disabled={!canRun} onClick={runNow}>
          {running ? "Starting…" : "Run pipeline"}
        </Button>
      </div>
      {scheduleOpen && (
        <ScheduleModal
          pipeline={pipeline}
          onClose={() => setScheduleOpen(false)}
          onSaved={(p) => { setPipeline(p); setScheduleOpen(false); toast("ok", p.schedule ? "Schedule saved" : "Schedule removed"); }}
        />
      )}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(240px,290px) minmax(340px,1fr) minmax(340px,1.1fr)", gap: 14, alignItems: "start" }}>
        {/* Steps sidebar */}
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, padding: 12, display: "flex", flexDirection: "column" }}>
          <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.04em", padding: "0 4px 10px" }}>Steps</div>
          <SidebarItem icon="input" title="Read source" sub={draft.source.remote ? remoteLabel(draft.source.remote, conns) || "no file yet" : draft.source.file ? draft.source.file.path || "no path yet" : draft.source.table || draft.source.query || "no source yet"} mode="Source"
            active={selected === "source"} onClick={() => setSelected("source")} />
          {draft.steps.map((s) => (
            <SidebarItem key={s.id} icon={STEP_META[s.kind].icon} title={STEP_META[s.kind].label} sub={stepSummary(s)} mode={STEP_META[s.kind].group === "code" ? s.kind.toUpperCase() : "Visual"}
              active={selected === s.id} onClick={() => setSelected(s.id)} error={stepError?.id === s.id ? stepError.message : undefined} />
          ))}
          <div style={{ position: "relative", padding: "4px 0 8px 0" }}>
            <button onClick={() => setAddMenu((v) => !v)} style={{ height: 28, padding: "0 10px", borderRadius: 6, border: "1px dashed var(--border-2)", background: "var(--surface)", color: "var(--text-2)", fontSize: 12, fontWeight: 500, display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <Icon name="add" style={{ fontSize: 16 }} />Add transform
            </button>
            {addMenu && (
              <div style={{ position: "absolute", left: 0, top: 34, width: 250, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, boxShadow: "var(--shadow-2)", padding: 6, zIndex: 10, display: "flex", flexDirection: "column", gap: 2 }}>
                <div style={{ fontSize: 11, color: "var(--text-3)", padding: "4px 8px", fontWeight: 500 }}>Visual</div>
                {STEP_KINDS.filter((k) => STEP_META[k].group === "visual").map((k) => (
                  <button key={k} onClick={() => addStep(k)} className="hover-surface2" style={{ height: 30, display: "flex", alignItems: "center", gap: 8, padding: "0 8px", border: "none", background: "transparent", borderRadius: 5, cursor: "pointer", textAlign: "left" }}>
                    <Icon name={STEP_META[k].icon} style={{ fontSize: 16, color: "var(--text-3)" }} />{STEP_META[k].label}
                  </button>
                ))}
                <div style={{ height: 1, background: "var(--border)", margin: "4px 0" }} />
                {STEP_KINDS.filter((k) => STEP_META[k].group === "code").map((k) => (
                  <button key={k} onClick={() => addStep(k)} className="hover-surface2" style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "6px 8px", border: "none", background: "transparent", borderRadius: 5, cursor: "pointer", textAlign: "left" }}>
                    <Icon name={STEP_META[k].icon} style={{ fontSize: 16, color: "var(--text-3)" }} />
                    <span><span style={{ display: "block" }}>{STEP_META[k].label}</span><span style={{ display: "block", fontSize: 11, color: "var(--text-3)" }}>{k === "sql" ? "DuckDB SQL over the previous step" : "transform(df) → DataFrame"}</span></span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <SidebarItem icon="output" title="Load" sub={`${draft.target.table || "no table yet"} · ${draft.target.mode}`} mode="Target"
            active={selected === "target"} onClick={() => setSelected("target")} />
        </div>

        {/* Editor panel */}
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, minWidth: 0 }}>
          {selected === "source" && (
            <>
              <PanelHeader icon="input" title="Read source" mode="Source" />
              <div style={{ padding: 14 }}><SourceEditor value={draft.source} onChange={(v) => setDraft({ ...draft, source: v })} conns={conns} /></div>
            </>
          )}
          {selected === "target" && (
            <>
              <PanelHeader icon="output" title="Load" mode="Target" />
              <div style={{ padding: 14 }}><TargetEditor value={draft.target} onChange={(v) => setDraft({ ...draft, target: v })} conns={conns} /></div>
            </>
          )}
          {selectedStep && (
            <>
              <PanelHeader icon={STEP_META[selectedStep.kind].icon} title={STEP_META[selectedStep.kind].label} mode={selectedStep.kind.toUpperCase()} onRemove={() => removeStep(selectedStep.id)} />
              {stepError?.id === selectedStep.id && <ErrorNote text={stepError.message} />}
              <div style={{ padding: 14 }}>
                <StepEditor key={selectedStep.id} step={selectedStep} onChange={(cfg) => updateStepConfig(selectedStep.id, cfg)} inputColumns={inputColumnsBefore(selectedStep.id)} />
              </div>
            </>
          )}
        </div>

        {/* Preview panel */}
        <div style={{ background: "var(--surface)", border: `1px solid ${incomplete ? "var(--err)" : "var(--border)"}`, boxShadow: incomplete ? "0 0 0 3px var(--err-soft)" : "none", borderRadius: 8, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: `1px solid ${incomplete ? "var(--err)" : "var(--border)"}`, flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600 }}>Preview</span>
            <span style={{ fontSize: 12, color: incomplete ? "var(--err)" : "var(--text-3)" }}>
              {previewError ? "last good result" : preview?.error ? `stopped before step ${stepNo(preview.error_step)}` : selected === "target" ? "final output" : selected === "source" ? "source columns" : "after this step"}
            </span>
            <div style={{ flex: 1 }} />
            {previewLoading && <Icon name="progress_activity" spin style={{ fontSize: 16, color: "var(--text-3)" }} />}
            {preview && !previewLoading && <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)" }}>{preview.sampled_rows} sample row{preview.sampled_rows === 1 ? "" : "s"} · {preview.columns.length} cols</span>}
          </div>
          {(previewError || preview?.error) && (
            <ErrorNote
              text={previewError || `Step ${stepNo(preview!.error_step)} (${stepLabel(preview!.error_step)}): ${preview!.error}`}
              sub={previewError ? (preview ? "Showing the last preview that worked." : undefined) : "The table below is the output just before that step, so it is not the complete result yet."}
              onClick={!previewError && preview?.error_step ? () => setSelected(preview.error_step!) : undefined}
            />
          )}
          {preview && (
            <div style={{ overflow: "auto", maxHeight: 560, opacity: previewError ? 0.55 : 1 }}>
              <table style={{ borderCollapse: "separate", borderSpacing: 0, fontSize: 12, minWidth: "100%" }}>
                <thead>
                  <tr>
                    {preview.columns.map((c) => (
                      <th key={c.name} style={{ position: "sticky", top: 0, background: "var(--surface-2)", borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", padding: "6px 8px", textAlign: "left", whiteSpace: "nowrap" }}>
                        <div style={{ fontFamily: "'Geist Mono',monospace", fontWeight: 500 }}>{c.name}</div>
                        <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 10, color: "var(--code-type)", fontWeight: 400 }}>{c.type}</div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((r, i) => (
                    <tr key={i}>
                      {preview.columns.map((c) => (
                        <td key={c.name} style={{ borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", padding: "0 8px", height: 28, whiteSpace: "nowrap", fontFamily: "'Geist Mono',monospace", color: r[c.name] == null ? "var(--text-3)" : "var(--text)" }}>
                          {r[c.name] == null ? "∅" : String(r[c.name])}
                        </td>
                      ))}
                    </tr>
                  ))}
                  {preview.rows.length === 0 && (
                    <tr><td colSpan={preview.columns.length || 1} style={{ padding: 14, color: "var(--text-3)" }}>No sample rows.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SidebarItem({ icon, title, sub, mode, active, onClick, error }: { icon: string; title: string; sub: string; mode: string; active: boolean; onClick: () => void; error?: string }) {
  const edge = error ? "var(--err)" : active ? "var(--accent)" : "transparent";
  return (
    <button onClick={onClick} title={error} className="hover-surface2" style={{ position: "relative", width: "100%", display: "flex", alignItems: "flex-start", gap: 10, padding: 6, marginBottom: 4, borderRadius: 7, border: `1px solid ${edge}`, background: error ? "var(--err-soft)" : active ? "var(--accent-soft)" : "transparent", cursor: "pointer", textAlign: "left" }}>
      <span style={{ width: 20, height: 20, marginTop: 1, borderRadius: 5, background: error ? "var(--err)" : active ? "var(--accent)" : "var(--surface-3)", color: error ? "#fff" : active ? "var(--accent-fg)" : "var(--text-2)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
        <Icon name={error ? "error" : icon} style={{ fontSize: 14 }} />
      </span>
      <span style={{ flex: 1, minWidth: 0 }}>
        <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <span style={{ fontWeight: 500, flex: 1 }}>{title}</span>
          <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 10, padding: "0 5px", borderRadius: 4, border: "1px solid var(--border-2)", color: "var(--text-3)" }}>{mode}</span>
        </span>
        <span style={{ display: "block", fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginTop: 2 }}>{sub}</span>
      </span>
    </button>
  );
}

function ErrorNote({ text, sub, onClick }: { text: string; sub?: string; onClick?: () => void }) {
  return (
    <div onClick={onClick} style={{ display: "flex", gap: 8, padding: "10px 14px", background: "var(--err-soft)", borderBottom: "1px solid var(--err)", fontSize: 12, cursor: onClick ? "pointer" : "default" }}>
      <Icon name="error" style={{ fontSize: 16, color: "var(--err)", flexShrink: 0 }} />
      <div style={{ minWidth: 0 }}>
        <div style={{ color: "var(--text)", overflowWrap: "anywhere" }}>{text}</div>
        {sub && <div style={{ color: "var(--text-2)", marginTop: 2 }}>{sub}</div>}
      </div>
    </div>
  );
}

function PanelHeader({ icon, title, mode, onRemove }: { icon: string; title: string; mode: string; onRemove?: () => void }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
      <span style={{ width: 22, height: 22, borderRadius: 5, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center" }}><Icon name={icon} style={{ fontSize: 15 }} /></span>
      <span style={{ fontWeight: 600, flex: 1 }}>{title}</span>
      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)" }}>{mode}</span>
      {onRemove && <IconButton icon="delete" title="Delete step" onClick={onRemove} style={{ color: "var(--text-3)" }} />}
    </div>
  );
}
