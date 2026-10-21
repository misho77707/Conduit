import { useState } from "react";
import { monitorApi, type CheckKind, type ConnectionHealth } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { Modal } from "../../ui/Modal";
import { Field } from "../../ui/Field";
import { Input, Select, TextArea } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ToggleRow } from "../../ui/Toggle";

const DEFAULTS: Record<CheckKind, { target: string; expect: string; targetLabel: string; expectLabel: string; placeholder: string }> = {
  http: { target: "https://", expect: "200", targetLabel: "URL", expectLabel: "Expected status", placeholder: "https://airflow.internal:8080/health" },
  tcp: { target: "", expect: "1000", targetLabel: "Host:port", expectLabel: "Max latency (ms)", placeholder: "kafka-1.internal:9092" },
  query: { target: "", expect: "", targetLabel: "", expectLabel: "Expected rows · optional", placeholder: "" },
  docker: { target: "", expect: "running", targetLabel: "Container name or ID", expectLabel: "Expect", placeholder: "my-container" },
};

export function ServiceCheckModal({ conns, onClose, onSaved }: { conns: ConnectionHealth[]; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const sqlConns = conns.filter((c) => c.type !== "mongodb" && c.category !== "storage");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<CheckKind>("http");
  const [target, setTarget] = useState(DEFAULTS.http.target);
  const [expect, setExpect] = useState(DEFAULTS.http.expect);
  const [connectionId, setConnectionId] = useState(sqlConns[0]?.id || "");
  const [query, setQuery] = useState("");
  const [intervalS, setIntervalS] = useState(30);
  const [verifyTls, setVerifyTls] = useState(true);
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);

  function pickKind(k: CheckKind) {
    setKind(k);
    setTarget(DEFAULTS[k].target);
    setExpect(DEFAULTS[k].expect);
    setErr("");
  }

  async function save() {
    setErr("");
    setSaving(true);
    try {
      await monitorApi.createCheck({
        name, kind, interval_s: intervalS, verify_tls: verifyTls,
        target: kind === "query" ? "" : target,
        connection_id: kind === "query" ? connectionId || null : null,
        query: kind === "query" ? query : null,
        expect,
      });
      toast("ok", `Added ${name}`, "First result in a few seconds.");
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not save the check");
    } finally {
      setSaving(false);
    }
  }

  const d = DEFAULTS[kind];
  const canSave = name.trim() && (kind === "query" ? connectionId && query.trim() : target.trim());

  return (
    <Modal
      title="Add service check"
      sub="Checked from the Conduit host on an interval. Only the latest result is kept."
      onClose={onClose}
      footer={
        <>
          <div style={{ flex: 1 }} />
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!canSave || saving} onClick={save}>{saving ? "Adding…" : "Add check"}</Button>
        </>
      }
    >
      <Field label="Name">
        <Input mono={false} value={name} onChange={(e) => setName(e.target.value)} placeholder="Airflow triggerer" />
      </Field>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Check type</span>
        <SegmentedControl options={[{ id: "http", label: "HTTP" }, { id: "tcp", label: "TCP" }, { id: "query", label: "Query" }, { id: "docker", label: "Docker" }]} value={kind} onChange={pickKind} />
      </div>

      {kind !== "query" ? (
        <Field label={d.targetLabel}>
          <Input value={target} onChange={(e) => setTarget(e.target.value)} placeholder={d.placeholder} />
        </Field>
      ) : (
        <>
          <Field label="Connection">
            <Select value={connectionId} onChange={(e) => setConnectionId(e.target.value)}>
              <option value="">Choose…</option>
              {sqlConns.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
          <Field label="Query · a single SELECT (or WITH ... SELECT)">
            <TextArea rows={2} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="SELECT 1" />
          </Field>
        </>
      )}

      <div style={{ display: "grid", gridTemplateColumns: kind === "http" || kind === "docker" ? "1fr 1fr" : "1fr", gap: 12 }}>
        <Field label={d.expectLabel}>
          {kind === "docker" ? (
            <Select value={expect} onChange={(e) => setExpect(e.target.value)}>
              <option value="running">Running</option>
              <option value="healthy">Healthy (has a HEALTHCHECK)</option>
            </Select>
          ) : (
            <Input mono value={expect} onChange={(e) => setExpect(e.target.value)} />
          )}
        </Field>
        <Field label="Interval">
          <Select value={intervalS} onChange={(e) => setIntervalS(Number(e.target.value))}>
            <option value={10}>10s</option>
            <option value={30}>30s</option>
            <option value={60}>60s</option>
            <option value={300}>5m</option>
          </Select>
        </Field>
      </div>
      {kind === "http" && <ToggleRow on={verifyTls} onClick={() => setVerifyTls((v) => !v)} title="Verify TLS certificate" />}
      {err && <div style={{ color: "var(--err)", fontSize: 12 }}>{err}</div>}
    </Modal>
  );
}
