import { useState } from "react";
import { pipelinesApi, type ConnectionOut, type SourceIn, type TargetIn } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { Modal } from "../../ui/Modal";
import { Field } from "../../ui/Field";
import { Input } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { SourceEditor, TargetEditor } from "./SourceTargetEditor";

export function NewPipelineModal({ conns, onClose, onCreated }: { conns: ConnectionOut[]; onClose: () => void; onCreated: (id: string) => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [source, setSource] = useState<SourceIn>({ connection_id: conns[0]?.id || null, table: "", where: "" });
  const [target, setTarget] = useState<TargetIn>({ connection_id: conns[0]?.id || "", table: "", mode: "append", keys: [], create: true, order_by: [], partition_by: null });
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);

  const canSave = name.trim() && target.connection_id && target.table.trim() && (source.file ? source.file.path.trim() : source.connection_id && (source.table?.trim() || source.query?.trim()));

  async function save() {
    setSaving(true);
    setErr("");
    try {
      const p = await pipelinesApi.create({ name: name.trim(), source, target, steps: [] });
      toast("ok", `Created ${p.name}`);
      onCreated(p.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not create the pipeline");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="New pipeline"
      sub="A saved read → transform → load recipe. Add transform steps after creating it."
      onClose={onClose}
      footer={
        <>
          <div style={{ flex: 1 }} />
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!canSave || saving} onClick={save}>{saving ? "Creating…" : "Create"}</Button>
        </>
      }
    >
      <Field label="Name">
        <Input mono value={name} onChange={(e) => setName(e.target.value)} placeholder="daily-transactions-clean" />
      </Field>
      <div>
        <div style={{ fontWeight: 600, marginBottom: 10 }}>Source</div>
        <SourceEditor value={source} onChange={setSource} conns={conns} />
      </div>
      <div>
        <div style={{ fontWeight: 600, marginBottom: 10 }}>Target</div>
        <TargetEditor value={target} onChange={setTarget} conns={conns} />
      </div>
      {err && <div style={{ color: "var(--err)", fontSize: 12 }}>{err}</div>}
    </Modal>
  );
}
