import { useState } from "react";
import { pipelinesApi, type PipelineOut } from "../../lib/api";
import { untilTime } from "../../lib/format";
import { Modal } from "../../ui/Modal";
import { Field } from "../../ui/Field";
import { Input } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { ToggleRow } from "../../ui/Toggle";
import { Icon } from "../../ui/Icon";

const PRESETS: { label: string; cron: string }[] = [
  { label: "Hourly", cron: "@hourly" },
  { label: "Daily at midnight", cron: "@daily" },
  { label: "Weekly", cron: "@weekly" },
  { label: "Monthly", cron: "@monthly" },
];

export function ScheduleModal({ pipeline, onClose, onSaved }: { pipeline: PipelineOut; onClose: () => void; onSaved: (p: PipelineOut) => void }) {
  const [cron, setCron] = useState(pipeline.schedule || "");
  const [enabled, setEnabled] = useState(pipeline.enabled);
  const [hasPreview, setHasPreview] = useState(!!pipeline.output_columns);
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  async function previewNow() {
    setPreviewing(true);
    setErr("");
    try {
      await pipelinesApi.preview(pipeline.id, 10, null); // full chain: upto_step omitted, so this caches output_columns
      setHasPreview(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Preview failed");
    } finally {
      setPreviewing(false);
    }
  }

  async function save() {
    setSaving(true);
    setErr("");
    try {
      const p = await pipelinesApi.update(pipeline.id, { schedule: cron.trim() || null, enabled });
      onSaved(p);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not save the schedule");
    } finally {
      setSaving(false);
    }
  }

  async function removeSchedule() {
    setSaving(true);
    setErr("");
    try {
      const p = await pipelinesApi.update(pipeline.id, { schedule: null });
      onSaved(p);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not remove the schedule");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="Schedule"
      sub="Runs this pipeline on its own clock. It never fires the moment you save it — only on the next slot."
      onClose={onClose}
      footer={
        <>
          {pipeline.schedule && <Button variant="ghost" onClick={removeSchedule} disabled={saving} style={{ color: "var(--text-2)" }}>Remove schedule</Button>}
          <div style={{ flex: 1 }} />
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={saving} onClick={save}>{saving ? "Saving…" : "Save"}</Button>
        </>
      }
    >
      <Field label="Cron expression" hint="5 fields (e.g. 0 2 * * * = 02:00 every day) or a shortcut like @daily.">
        <Input mono value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 2 * * *" />
      </Field>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {PRESETS.map((p) => (
          <button key={p.cron} onClick={() => setCron(p.cron)} style={{ height: 26, padding: "0 10px", borderRadius: 6, border: `1px solid ${cron === p.cron ? "var(--accent)" : "var(--border-2)"}`, background: "var(--surface)", color: cron === p.cron ? "var(--accent)" : "var(--text-2)", fontSize: 12, cursor: "pointer" }}>
            {p.label}
          </button>
        ))}
      </div>
      <ToggleRow on={enabled} onClick={() => setEnabled((v) => !v)} title="Enabled" sub="Disabled pipelines keep their schedule but never fire." />
      {pipeline.schedule && pipeline.enabled && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: "var(--text-2)" }}>
          <Icon name="schedule" style={{ fontSize: 15, color: "var(--text-3)" }} />
          Currently next runs {untilTime(pipeline.next_run_at)}
        </div>
      )}
      {!hasPreview && (
        <div style={{ display: "flex", gap: 8, padding: "10px 12px", borderRadius: 6, background: "var(--warn-soft)", fontSize: 12, alignItems: "flex-start" }}>
          <Icon name="warning" style={{ fontSize: 15, color: "var(--warn)", marginTop: 1 }} />
          <div style={{ display: "flex", flexDirection: "column", gap: 6, flex: 1 }}>
            <span>This pipeline has no cached preview yet, so it won&rsquo;t actually fire even while enabled.</span>
            <Button height={26} padX={10} disabled={previewing} onClick={previewNow} style={{ alignSelf: "flex-start", fontSize: 12 }}>
              {previewing ? "Previewing…" : "Run a full preview now"}
            </Button>
          </div>
        </div>
      )}
      {err && <div style={{ color: "var(--err)", fontSize: 12 }}>{err}</div>}
    </Modal>
  );
}
