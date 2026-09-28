import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { connectionsApi, metaApi, type ConnectionOut, type DbTypeOut, type TestResultOut } from "../lib/api";
import { useTheme } from "../lib/theme";
import { useToast } from "../lib/toast";
import { dbTile } from "../lib/format";
import { Modal } from "../ui/Modal";
import { Field } from "../ui/Field";
import { Input, Select, TextArea } from "../ui/Input";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { ToggleRow } from "../ui/Toggle";

type Form = Record<string, string | boolean | undefined>;

export function AddConnectionModal({ connection, initialTypes, onClose, onSaved }: { connection?: ConnectionOut; initialTypes?: DbTypeOut[]; onClose: () => void; onSaved: () => void }) {
  const isEdit = !!connection;
  const { theme } = useTheme();
  const toast = useToast();
  const [types, setTypes] = useState<DbTypeOut[]>(initialTypes || []);
  const [step, setStep] = useState<1 | 2>(isEdit ? 2 : 1);
  const [type, setType] = useState<DbTypeOut | null>(null);
  const [form, setForm] = useState<Form>({});
  const [test, setTest] = useState<{ state: "idle" | "testing" | "ok" | "fail"; result?: TestResultOut }>({ state: "idle" });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    // The caller (Connections.tsx) already loads this list for its own use; reuse it via `initialTypes` so
    // the modal renders immediately instead of a blank flash while its own copy of the same request is in
    // flight — only fetch here as a fallback if the caller didn't have it ready yet.
    if (initialTypes && initialTypes.length) return;
    metaApi.dbTypes().then(setTypes).catch(() => toast("err", "Could not load database types"));
  }, []);

  // Editing: the type is fixed (PATCH /connections/{id} can't change it) — resolve it once the types list
  // loads and seed the form from the saved config. Secret fields (password / uri) can't be read back from
  // the API at all, so they start blank; leaving them blank on save keeps the stored value (matches the
  // backend's merge behavior in ConnectionPatch). useLayoutEffect (not useEffect) so this resolves before
  // the browser paints — with useEffect the empty pre-population frame was visibly flashing on open.
  useLayoutEffect(() => {
    if (!isEdit || !connection || type || !types.length) return;
    const t = types.find((x) => x.type === connection.type);
    if (!t) { toast("err", "Unknown connection type", connection.type); onClose(); return; }
    setType(t);
    const c = connection.config || {};
    if (t.kind === "host") {
      setForm({
        name: connection.name, host: String(c.host ?? ""),
        port: c.port != null ? String(c.port) : t.default_port ? String(t.default_port) : "",
        database: String(c.database ?? ""), user: String(c.user ?? ""), password: "",
        ssl: String(c.ssl ?? "disable"), extra: String(c.extra ?? ""),
      });
    } else if (t.kind === "uri") {
      setForm({ name: connection.name, uri: "", database: String(c.database ?? ""), auth_source: String(c.auth_source ?? ""), tls: !!c.tls });
    } else {
      setForm({ name: connection.name, path: String(c.path ?? ""), read_only: !!c.read_only, create_if_missing: !!c.create_if_missing });
    }
  }, [types, isEdit, connection, type]);

  const pick = (t: DbTypeOut) => {
    setType(t);
    setTest({ state: "idle" });
    setForm({ name: "", port: t.default_port ? String(t.default_port) : "", ssl: "disable", database: "", user: "", read_only: false, create_if_missing: false, tls: false });
  };

  const set = (patch: Partial<Form>) => { setForm((f) => ({ ...f, ...patch })); setTest({ state: "idle" }); };

  const { config, secrets } = useMemo(() => buildPayload(type, form), [type, form]);

  async function runTest() {
    if (!type) return;
    setTest({ state: "testing" });
    try {
      const result = await connectionsApi.testUnsaved({ type: type.type, config, secrets });
      setTest({ state: result.ok ? "ok" : "fail", result });
    } catch (e) {
      setTest({ state: "fail", result: { ok: false, status: "err", latency_ms: null, version: null, detail: null, error: e instanceof Error ? e.message : "Test failed", degraded: false } });
    }
  }

  async function save() {
    if (!type) return;
    setSaving(true);
    try {
      if (isEdit && connection) {
        const name = String(form.name || "").trim() || connection.name;
        const conn = await connectionsApi.update(connection.id, { name, config, secrets });
        toast("ok", `Updated ${conn.name}`, test.state === "ok" ? "Connection verified." : "Not re-tested — run a test from the list.");
      } else {
        const name = String(form.name || "").trim() || type.type;
        const conn = await connectionsApi.create({ name, type: type.type, config, secrets });
        toast("ok", `Saved ${conn.name}`, test.state === "ok" ? "Connection verified and added to background checks." : "Saved without testing. Run a test from the list.");
      }
      onSaved();
    } catch (e) {
      toast("err", isEdit ? "Could not save changes" : "Could not save connection", e instanceof Error ? e.message : undefined);
    } finally {
      setSaving(false);
    }
  }

  const dark = theme === "dark";

  return (
    <Modal
      title={isEdit ? "Edit connection" : "Add connection"}
      sub={isEdit ? `Update the settings for ${connection?.name}.` : step === 2 ? "Step 2 of 2 · Connection details" : "Step 1 of 2 · Choose a database type"}
      onClose={onClose}
      footer={
        <>
          {step === 2 && (
            <Button icon="bolt" onClick={runTest} disabled={test.state === "testing"}>
              Test connection
            </Button>
          )}
          <div style={{ flex: 1 }} />
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          {step === 1 && <Button variant="primary" disabled={!type} onClick={() => setStep(2)}>Continue</Button>}
          {step === 2 && <Button variant="primary" disabled={saving} onClick={save}>{saving ? "Saving…" : isEdit ? "Save changes" : "Save connection"}</Button>}
        </>
      }
    >
      {step === 1 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(190px,1fr))", gap: 10 }}>
          {types.map((t) => {
            const tile = dbTile(t.type, dark);
            const active = type?.type === t.type;
            return (
              <button
                key={t.type}
                onClick={() => pick(t)}
                className="hover-surface2"
                style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 10, padding: 14, borderRadius: 8, border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`, boxShadow: active ? "0 0 0 3px var(--accent-soft)" : "none", background: "var(--surface)", cursor: "pointer", textAlign: "left" }}
              >
                <span style={{ width: 34, height: 34, borderRadius: 8, background: tile.tbg, color: tile.tfg, fontFamily: "'Geist Mono',monospace", fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center" }}>{t.abbr}</span>
                <span>
                  <span style={{ display: "block", fontWeight: 500 }}>{t.label}</span>
                  <span style={{ display: "block", fontSize: 12, color: "var(--text-3)", marginTop: 2 }}>{t.description}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}

      {step === 2 && type && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", borderRadius: 8, background: "var(--surface-2)" }}>
            {(() => { const tile = dbTile(type.type, dark); return <span style={{ width: 28, height: 28, borderRadius: 6, background: tile.tbg, color: tile.tfg, fontFamily: "'Geist Mono',monospace", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center" }}>{type.abbr}</span>; })()}
            <span style={{ flex: 1, fontWeight: 500 }}>{type.label}</span>
            {!isEdit && <Button variant="ghost" height={26} padX={8} onClick={() => setStep(1)} style={{ color: "var(--accent)", fontSize: 12 }}>Change type</Button>}
          </div>
          <Field label="Connection name" hint="Used to pick this connection elsewhere in Conduit, e.g. prod-clickhouse">
            <Input value={String(form.name || "")} onChange={(e) => set({ name: e.target.value })} />
          </Field>

          {type.kind === "host" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0,3fr) minmax(0,1fr)", gap: 12 }}>
                <Field label="Host"><Input value={String(form.host || "")} onChange={(e) => set({ host: e.target.value })} placeholder="db.internal" /></Field>
                <Field label="Port"><Input value={String(form.port || "")} onChange={(e) => set({ port: e.target.value })} /></Field>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3,minmax(0,1fr))", gap: 12 }}>
                <Field label="Database"><Input value={String(form.database || "")} onChange={(e) => set({ database: e.target.value })} /></Field>
                <Field label="User"><Input value={String(form.user || "")} onChange={(e) => set({ user: e.target.value })} /></Field>
                <Field label="Password" hint={isEdit ? "Blank keeps the current password" : undefined}>
                  <Input type="password" mono={false} value={String(form.password || "")} onChange={(e) => set({ password: e.target.value })} placeholder={isEdit ? "••••••••" : undefined} />
                </Field>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,2fr)", gap: 12 }}>
                <Field label="SSL mode">
                  <Select value={String(form.ssl || "disable")} onChange={(e) => set({ ssl: e.target.value })}>
                    <option value="disable">disable</option>
                    <option value="require">require</option>
                    <option value="verify-ca">verify-ca</option>
                    <option value="verify-full">verify-full</option>
                  </Select>
                </Field>
                <Field label="Extra options · key=value, one per line">
                  <TextArea rows={2} value={String(form.extra || "")} onChange={(e) => set({ extra: e.target.value })} />
                </Field>
              </div>
            </>
          )}

          {type.kind === "uri" && (
            <>
              <Field label="Connection string" hint={isEdit ? "Blank keeps the current connection string. mongodb:// or mongodb+srv://" : "mongodb:// or mongodb+srv:// — the password is stripped and stored encrypted."}>
                <Input value={String(form.uri || "")} onChange={(e) => set({ uri: e.target.value })} placeholder={isEdit ? "unchanged" : undefined} />
              </Field>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 12 }}>
                <Field label="Default database"><Input value={String(form.database || "")} onChange={(e) => set({ database: e.target.value })} /></Field>
                <Field label="Auth source"><Input value={String(form.auth_source || "")} onChange={(e) => set({ auth_source: e.target.value })} /></Field>
              </div>
              <ToggleRow on={!!form.tls} onClick={() => set({ tls: !form.tls })} title="Require TLS" />
            </>
          )}

          {type.kind === "file" && (
            <>
              <Field label="Database file path" hint="Path on the Conduit host, inside an allowed data folder.">
                <Input value={String(form.path || "")} onChange={(e) => set({ path: e.target.value })} />
              </Field>
              <ToggleRow on={!!form.read_only} onClick={() => set({ read_only: !form.read_only })} title="Open read-only" />
              <ToggleRow on={!!form.create_if_missing} onClick={() => set({ create_if_missing: !form.create_if_missing })} title="Create file if missing" />
            </>
          )}

          {test.state === "testing" && (
            <StatusBox tone="accent" icon="progress_activity" spin title="Connecting…" />
          )}
          {test.state === "ok" && test.result && (
            <StatusBox tone="ok" icon="check_circle" title={`Connected${test.result.version ? ` · ${test.result.version}` : ""}${test.result.latency_ms != null ? ` · ${test.result.latency_ms} ms` : ""}`} sub={test.result.detail || undefined} />
          )}
          {test.state === "fail" && (
            <StatusBox tone="err" icon="error" title="Connection failed" sub={test.result?.error || undefined} />
          )}
        </>
      )}
    </Modal>
  );
}

function StatusBox({ tone, icon, title, sub, spin }: { tone: "accent" | "ok" | "err"; icon: string; title: string; sub?: string; spin?: boolean }) {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: 12, borderRadius: 8, background: `var(--${tone}-soft)`, color: `var(--${tone})` }}>
      <Icon name={icon} spin={spin} />
      <div>
        <div style={{ fontWeight: 500 }}>{title}</div>
        {sub && <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, marginTop: 3, color: "var(--text-2)" }}>{sub}</div>}
      </div>
    </div>
  );
}

function buildPayload(type: DbTypeOut | null, form: Form): { config: Record<string, unknown>; secrets: Record<string, unknown> } {
  if (!type) return { config: {}, secrets: {} };
  const config: Record<string, unknown> = {};
  const secrets: Record<string, unknown> = {};
  const isSecret = (k: string) => type.secret_fields.includes(k);
  const put = (k: string, v: unknown) => { if (isSecret(k)) secrets[k] = v; else config[k] = v; };

  if (type.kind === "host") {
    put("host", form.host || "");
    if (form.port) put("port", Number(form.port));
    put("database", form.database || "");
    put("user", form.user || "");
    if (form.password) put("password", form.password);
    put("ssl", form.ssl || "disable");
    if (form.extra) put("extra", form.extra);
  } else if (type.kind === "uri") {
    if (form.uri) put("uri", form.uri);
    put("database", form.database || "");
    if (form.auth_source) put("auth_source", form.auth_source);
    config.tls = !!form.tls;
  } else if (type.kind === "file") {
    put("path", form.path || "");
    config.read_only = !!form.read_only;
    config.create_if_missing = !!form.create_if_missing;
  }
  return { config, secrets };
}
