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
        driver: connection.driver ?? undefined,
        ch_collection: String(c.ch_collection ?? ""), ch_address: String(c.ch_address ?? ""),
      });
    } else if (t.kind === "uri") {
      setForm({ name: connection.name, uri: "", database: String(c.database ?? ""), auth_source: String(c.auth_source ?? ""), tls: !!c.tls });
    } else if (t.kind === "s3") {
      setForm({ name: connection.name, bucket: String(c.bucket ?? ""), region: String(c.region ?? ""), prefix: String(c.prefix ?? ""),
        endpoint_url: String(c.endpoint_url ?? ""), path_style: !!c.path_style });
    } else if (t.kind === "sftp") {
      setForm({ name: connection.name, host: String(c.host ?? ""), port: c.port != null ? String(c.port) : "22", user: String(c.user ?? ""),
        base_path: String(c.base_path ?? ""), host_key_sha256: String(c.host_key_sha256 ?? "") });
    } else if (t.kind === "http") {
      setForm({ name: connection.name, base_url: String(c.base_url ?? "") });
    } else {
      setForm({ name: connection.name, path: String(c.path ?? ""), read_only: !!c.read_only, create_if_missing: !!c.create_if_missing });
    }
  }, [types, isEdit, connection, type]);

  const pick = (t: DbTypeOut) => {
    setType(t);
    setTest({ state: "idle" });
    setForm({ name: "", port: t.default_port ? String(t.default_port) : "", ssl: "disable", database: "", user: "", read_only: false, create_if_missing: false, tls: false,
      region: t.kind === "s3" ? "us-east-1" : "", path_style: false, driver: t.drivers.length ? "auto" : undefined });
  };

  const set = (patch: Partial<Form>) => { setForm((f) => ({ ...f, ...patch })); setTest({ state: "idle" }); };

  const { config, secrets } = useMemo(() => buildPayload(type, form), [type, form]);
  const driver = type?.drivers.length ? String(form.driver || "auto") : undefined;
  // what a successful test discovered (an SFTP host key: trusted on first use) is saved with the connection
  const learned = test.state === "ok" ? test.result?.learned || {} : {};

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
        const conn = await connectionsApi.update(connection.id, { name, driver, config: { ...config, ...learned }, secrets });
        toast("ok", `Updated ${conn.name}`, test.state === "ok" ? "Connection verified." : "Not re-tested — run a test from the list.");
      } else {
        const name = String(form.name || "").trim() || type.type;
        const conn = await connectionsApi.create({ name, type: type.type, driver, config: { ...config, ...learned }, secrets });
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
      sub={isEdit ? `Update the settings for ${connection?.name}.` : step === 2 ? "Step 2 of 2 · Connection details" : "Step 1 of 2 · Choose a database or file storage"}
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
      {step === 1 && ([["database", "Databases"], ["storage", "File storage"]] as const).map(([cat, heading]) => (
        <div key={cat} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.04em" }}>{heading}</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(190px,1fr))", gap: 10 }}>
          {types.filter((t) => (t.category || "database") === cat).map((t) => {
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
        </div>
      ))}

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
              {type.drivers.length > 0 && <DriverField options={type.drivers} value={String(form.driver || "auto")} onChange={(v) => set({ driver: v })} />}
              {type.ch_pull && (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 12 }}>
                  <Field label="ClickHouse named collection for direct pulls · optional"
                    hint="Made by a ClickHouse admin with this database's host, port, user, password and database; Conduit never sees that password.">
                    <Input value={String(form.ch_collection || "")} onChange={(e) => set({ ch_collection: e.target.value })} placeholder="pg_prod" />
                  </Field>
                  <Field label="Address as ClickHouse sees it · optional" hint="host or host:port, when ClickHouse reaches this database by another address than the collection's.">
                    <Input value={String(form.ch_address || "")} onChange={(e) => set({ ch_address: e.target.value })} placeholder="db.internal:5432" />
                  </Field>
                </div>
              )}
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

          {type.kind === "s3" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0,2fr) minmax(0,1fr)", gap: 12 }}>
                <Field label="Bucket"><Input value={String(form.bucket || "")} onChange={(e) => set({ bucket: e.target.value })} placeholder="my-data-lake" /></Field>
                <Field label="Region"><Input value={String(form.region || "")} onChange={(e) => set({ region: e.target.value })} placeholder="us-east-1" /></Field>
              </div>
              <Field label="Prefix · optional" hint="A folder inside the bucket that every path is relative to, e.g. exports/daily">
                <Input value={String(form.prefix || "")} onChange={(e) => set({ prefix: e.target.value })} />
              </Field>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 12 }}>
                <Field label="Access key ID" hint={isEdit ? "Blank keeps the current key" : "Leave both keys empty to use the server's IAM role"}>
                  <Input value={String(form.access_key_id || "")} onChange={(e) => set({ access_key_id: e.target.value })} placeholder={isEdit ? "unchanged" : "AKIA…"} />
                </Field>
                <Field label="Secret access key" hint={isEdit ? "Blank keeps the current secret" : undefined}>
                  <Input type="password" mono={false} value={String(form.secret_access_key || "")} onChange={(e) => set({ secret_access_key: e.target.value })} placeholder={isEdit ? "••••••••" : undefined} />
                </Field>
              </div>
              <Field label="Session token · optional" hint="Only for temporary (STS) credentials">
                <Input type="password" mono={false} value={String(form.session_token || "")} onChange={(e) => set({ session_token: e.target.value })} placeholder={isEdit ? "unchanged" : undefined} />
              </Field>
              <Field label="Endpoint URL · optional" hint="For S3-compatible storage: MinIO (http://minio:9000), Cloudflare R2, Wasabi … Empty = AWS.">
                <Input value={String(form.endpoint_url || "")} onChange={(e) => set({ endpoint_url: e.target.value })} placeholder="https://s3.example.com" />
              </Field>
              <ToggleRow on={!!form.path_style} onClick={() => set({ path_style: !form.path_style })} title="Path-style addressing" sub="endpoint/bucket/key instead of bucket.endpoint/key. MinIO usually needs this." />
            </>
          )}

          {type.kind === "sftp" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0,3fr) minmax(0,1fr)", gap: 12 }}>
                <Field label="Host"><Input value={String(form.host || "")} onChange={(e) => set({ host: e.target.value })} placeholder="sftp.partner.com" /></Field>
                <Field label="Port"><Input value={String(form.port || "")} onChange={(e) => set({ port: e.target.value })} /></Field>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 12 }}>
                <Field label="User"><Input value={String(form.user || "")} onChange={(e) => set({ user: e.target.value })} /></Field>
                <Field label="Base path · optional" hint="/abs/path, or relative to the user's home">
                  <Input value={String(form.base_path || "")} onChange={(e) => set({ base_path: e.target.value })} placeholder="/upload" />
                </Field>
              </div>
              <Field label="Password" hint={isEdit ? "Blank keeps the current password" : "A password, a private key, or both"}>
                <Input type="password" mono={false} value={String(form.password || "")} onChange={(e) => set({ password: e.target.value })} placeholder={isEdit ? "••••••••" : undefined} />
              </Field>
              <Field label="Private key · optional" hint={isEdit ? "Blank keeps the current key. PEM or OpenSSH format (Ed25519, ECDSA, RSA)." : "Paste the whole key: PEM or OpenSSH format (Ed25519, ECDSA, RSA)."}>
                <TextArea rows={4} value={String(form.private_key || "")} onChange={(e) => set({ private_key: e.target.value })} placeholder={isEdit ? "unchanged" : "-----BEGIN OPENSSH PRIVATE KEY-----"} style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11 }} />
              </Field>
              <Field label="Key passphrase · optional">
                <Input type="password" mono={false} value={String(form.passphrase || "")} onChange={(e) => set({ passphrase: e.target.value })} placeholder={isEdit ? "unchanged" : undefined} />
              </Field>
              <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "10px 12px", borderRadius: 8, background: "var(--surface-2)", fontSize: 12 }}>
                <Icon name="fingerprint" style={{ color: "var(--text-3)" }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  {form.host_key_sha256 ? (
                    <><div style={{ fontWeight: 500 }}>Trusted host key</div><div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)", overflowWrap: "anywhere" }}>{String(form.host_key_sha256)}</div></>
                  ) : learned.host_key_sha256 ? (
                    <><div style={{ fontWeight: 500 }}>Host key seen for the first time: it will be trusted when you save</div><div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)", overflowWrap: "anywhere" }}>{String(learned.host_key_sha256)}</div></>
                  ) : (
                    <span style={{ color: "var(--text-2)" }}>No host key stored yet. The first successful connection trusts the server&rsquo;s key; a later change is refused.</span>
                  )}
                </div>
                {form.host_key_sha256 && <Button height={26} padX={8} style={{ fontSize: 12 }} onClick={() => set({ host_key_sha256: "" })}>Forget</Button>}
              </div>
            </>
          )}

          {type.kind === "http" && (
            <>
              <Field label="Base URL" hint="Files are addressed relative to this URL. The secret headers are only ever sent to this host.">
                <Input value={String(form.base_url || "")} onChange={(e) => set({ base_url: e.target.value })} placeholder="https://files.example.com/exports/" />
              </Field>
              <Field label="Headers" hint={isEdit ? "Blank keeps the current headers. One Name: value per line." : "One Name: value per line, e.g. Authorization: Bearer … Stored encrypted."}>
                <TextArea rows={3} value={String(form.headers || "")} onChange={(e) => set({ headers: e.target.value })} placeholder={isEdit ? "unchanged" : "Authorization: Bearer eyJ…"} style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11 }} />
              </Field>
              <div style={{ fontSize: 12, color: "var(--text-3)" }}>Public files need no connection: paste their URL straight into Upload &amp; Load.</div>
            </>
          )}

          {test.state === "testing" && (
            <StatusBox tone="accent" icon="progress_activity" spin title="Connecting…" />
          )}
          {test.state === "ok" && test.result && (
            <StatusBox tone="ok" icon="check_circle" title={`Connected${test.result.version ? ` · ${test.result.version}` : ""}${test.result.latency_ms != null ? ` · ${test.result.latency_ms} ms` : ""}`} sub={test.result.detail || undefined} />
          )}
          {test.state === "ok" && test.result?.note && (
            <StatusBox tone="warn" icon="warning" title="Driver" sub={test.result.note} />
          )}
          {test.state === "fail" && (
            <StatusBox tone="err" icon="error" title="Connection failed" sub={test.result?.error || undefined} />
          )}
        </>
      )}
    </Modal>
  );
}

/** The connection's driver: automatic (the fastest that can run each job, else the classic one) or one fixed option. */
function DriverField({ options, value, onChange }: { options: DbTypeOut["drivers"]; value: string; onChange: (v: string) => void }) {
  const picked = options.find((o) => o.id === value);
  const fast = options.find((o) => !o.classic);
  const classic = options.find((o) => o.classic);
  const hint = !picked
    ? `Each job uses ${fast?.label ?? "the fastest option"} when it can run it, else ${classic?.label ?? "the classic driver"}; the job says why.`
    : picked.classic ? picked.note : `${picked.note} A job it can't run is refused, not moved to ${classic?.label ?? "the classic driver"}.`;
  return (
    <Field label="Driver" hint={hint}>
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="auto">Automatic: the fastest that can run each job</option>
        {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
      </Select>
    </Field>
  );
}

function StatusBox({ tone, icon, title, sub, spin }: { tone: "accent" | "ok" | "err" | "warn"; icon: string; title: string; sub?: string; spin?: boolean }) {
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
    if (type.ch_pull) {  // always sent, so clearing a field on edit clears the stored value
      put("ch_collection", String(form.ch_collection || "").trim());
      put("ch_address", String(form.ch_address || "").trim());
    }
  } else if (type.kind === "uri") {
    if (form.uri) put("uri", form.uri);
    put("database", form.database || "");
    if (form.auth_source) put("auth_source", form.auth_source);
    config.tls = !!form.tls;
  } else if (type.kind === "s3") {
    put("bucket", String(form.bucket || "").trim());
    put("region", String(form.region || "").trim() || "us-east-1");
    put("prefix", String(form.prefix || "").trim());
    put("endpoint_url", String(form.endpoint_url || "").trim());
    config.path_style = !!form.path_style;
    for (const k of ["access_key_id", "secret_access_key", "session_token"]) if (form[k]) put(k, String(form[k]).trim());
  } else if (type.kind === "sftp") {
    put("host", String(form.host || "").trim());
    if (form.port) put("port", Number(form.port));
    put("user", String(form.user || "").trim());
    put("base_path", String(form.base_path || "").trim());
    // "" after Forget clears the stored fingerprint; the next connection trusts the server's key again
    if (form.host_key_sha256 !== undefined) config.host_key_sha256 = form.host_key_sha256 || null;
    for (const k of ["password", "private_key", "passphrase"]) if (form[k]) put(k, form[k]);
  } else if (type.kind === "http") {
    put("base_url", String(form.base_url || "").trim());
    if (form.headers) put("headers", form.headers);
  } else if (type.kind === "file") {
    put("path", form.path || "");
    config.read_only = !!form.read_only;
    config.create_if_missing = !!form.create_if_missing;
  }
  return { config, secrets };
}
