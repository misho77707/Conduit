import { useState } from "react";
import { alertsApi, ApiError, type AlertChannelOut, type AlertKind, type AlertSendResult } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { Modal } from "../../ui/Modal";
import { Field } from "../../ui/Field";
import { Input, TextArea } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { Icon } from "../../ui/Icon";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ToggleRow } from "../../ui/Toggle";
import { KIND } from "./alertText";

type Tls = "starttls" | "ssl" | "none";
const PORT: Record<Tls, number> = { starttls: 587, ssl: 465, none: 25 };

/** Headers typed one per line, "Name: value". */
function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const i = line.indexOf(":");
    if (i < 1) throw new Error(`"${line}" is not a header (Name: value)`);
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

export function ChannelModal({ channel, onClose, onSaved }: { channel?: AlertChannelOut; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const edit = !!channel;
  const cfg = (channel?.config || {}) as Record<string, any>;
  const [kind, setKind] = useState<AlertKind>(channel?.kind || "telegram");
  const [name, setName] = useState(channel?.name || "");
  const [enabled, setEnabled] = useState(channel?.enabled ?? true);
  const [chatId, setChatId] = useState(String(cfg.chat_id || ""));
  const [host, setHost] = useState(String(cfg.host || ""));
  const [tls, setTls] = useState<Tls>((cfg.tls as Tls) || "starttls");
  const [port, setPort] = useState(String(cfg.port || PORT.starttls));
  const [user, setUser] = useState(String(cfg.user || ""));
  const [from, setFrom] = useState(String(cfg.from || ""));
  const [to, setTo] = useState(((cfg.to as string[]) || []).join(", "));
  const [secret, setSecret] = useState<Record<string, string>>({}); // typed secrets; blank = keep the stored one
  const [clear, setClear] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<AlertSendResult | null>(null);
  const stored = new Set(channel?.secrets_set || []);
  const setS = (k: string) => (e: { target: { value: string } }) => setSecret((s) => ({ ...s, [k]: e.target.value }));
  const keep = (k: string) => (edit && stored.has(k) ? "saved: leave blank to keep it" : "");

  function build() {
    const config: Record<string, unknown> =
      kind === "telegram" ? { chat_id: chatId.trim() }
        : kind === "email" ? { host: host.trim(), port: Number(port) || PORT[tls], tls, user: user.trim(), from: from.trim(), to }
          : {};
    const secrets: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(secret)) {
      if (!v.trim()) continue;
      secrets[k] = k === "headers" ? parseHeaders(v) : v.trim();
    }
    return { name: name.trim(), kind, enabled, config, secrets };
  }

  async function runTest() {
    setErr("");
    setTest(null);
    setBusy(true);
    try { setTest(await alertsApi.testUnsaved({ ...build(), id: channel?.id || null })); }
    catch (e) { setErr(e instanceof Error ? e.message : "Could not test"); }
    finally { setBusy(false); }
  }

  async function save() {
    setErr("");
    if (!name.trim()) return setErr("Give the channel a name.");
    setBusy(true);
    try {
      const body = build();
      if (channel) await alertsApi.updateChannel(channel.id, { name: body.name, enabled, config: body.config, secrets: body.secrets, clear_secrets: clear });
      else await alertsApi.createChannel(body);
      toast("ok", edit ? `Saved ${body.name}` : `Added ${body.name}`);
      onSaved();
    } catch (e) {
      setErr(e instanceof ApiError || e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  const removable = (k: string, label: string) => edit && stored.has(k) && (
    <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, color: "var(--text-2)" }}>
      <input type="checkbox" checked={clear.includes(k)} onChange={(e) => setClear((c) => (e.target.checked ? [...c, k] : c.filter((x) => x !== k)))} />
      Remove the saved {label}
    </label>
  );

  return (
    <Modal width={560} title={edit ? `Edit ${channel!.name}` : "Add channel"} sub="Where alert messages go. Secrets are stored encrypted and never shown again." onClose={onClose}
      footer={<>
        <Button icon="send" disabled={busy} onClick={runTest}>Send test</Button>
        <div style={{ flex: 1 }} />
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={busy} onClick={save}>{busy ? "Working…" : edit ? "Save" : "Add channel"}</Button>
      </>}>
      {!edit && (
        <SegmentedControl value={kind} onChange={(k) => { setKind(k); setTest(null); }}
          options={(Object.keys(KIND) as AlertKind[]).map((k) => ({ id: k, label: KIND[k].label }))} />
      )}
      <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder={kind === "slack" ? "#data-alerts" : kind === "email" ? "data team mail" : kind === "telegram" ? "Telegram ops" : "pager webhook"} /></Field>

      {kind === "telegram" && <>
        <Field label="Bot token" hint={keep("bot_token") || "From @BotFather (/newbot). Add the bot to the chat or channel first."}>
          <Input type="password" mono value={secret.bot_token || ""} onChange={setS("bot_token")} placeholder={keep("bot_token") || "123456789:AA…"} autoComplete="off" />
        </Field>
        <Field label="Chat ID" hint="A group or channel ID (-100…), a user ID, or @channelname for a public channel.">
          <Input mono value={chatId} onChange={(e) => setChatId(e.target.value)} placeholder="-1001234567890" />
        </Field>
      </>}

      {kind === "slack" && (
        <Field label="Incoming webhook URL" hint={keep("webhook_url") || "Slack app → Incoming Webhooks → the channel's https://hooks.slack.com/… URL."}>
          <Input type="password" mono value={secret.webhook_url || ""} onChange={setS("webhook_url")} placeholder={keep("webhook_url") || "https://hooks.slack.com/services/…"} autoComplete="off" />
        </Field>
      )}

      {kind === "email" && <>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 110px", gap: 10 }}>
          <Field label="SMTP host"><Input mono value={host} onChange={(e) => setHost(e.target.value)} placeholder="smtp.example.com" /></Field>
          <Field label="Port"><Input mono value={port} onChange={(e) => setPort(e.target.value)} /></Field>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Encryption</span>
          <SegmentedControl small value={tls} onChange={(t) => { setTls(t); setPort(String(PORT[t])); }}
            options={[{ id: "starttls", label: "STARTTLS" }, { id: "ssl", label: "SSL / TLS" }, { id: "none", label: "None" }]} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <Field label="User · optional"><Input mono value={user} onChange={(e) => setUser(e.target.value)} autoComplete="off" /></Field>
          <Field label="Password · optional"><Input type="password" value={secret.password || ""} onChange={setS("password")} placeholder={keep("password")} autoComplete="new-password" /></Field>
        </div>
        <Field label="From"><Input mono value={from} onChange={(e) => setFrom(e.target.value)} placeholder="conduit@example.com" /></Field>
        <Field label="To" hint="One or more addresses, separated by commas."><Input mono value={to} onChange={(e) => setTo(e.target.value)} placeholder="data-team@example.com" /></Field>
      </>}

      {kind === "webhook" && <>
        <Field label="URL" hint={keep("url") || "Conduit POSTs a JSON payload (see the README) to this address."}>
          <Input type="password" mono value={secret.url || ""} onChange={setS("url")} placeholder={keep("url") || "https://example.com/hooks/conduit"} autoComplete="off" />
        </Field>
        <Field label="Headers · optional" hint={keep("headers") || "One per line: Name: value (e.g. Authorization: Bearer …). Stored as a secret."}>
          <TextArea rows={2} value={secret.headers || ""} onChange={setS("headers")} placeholder={keep("headers") || "Authorization: Bearer …"} style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12 }} />
        </Field>
        {removable("headers", "headers")}
        <Field label="Signing secret · optional" hint={keep("signing_secret") || "Adds X-Conduit-Signature: sha256=<HMAC-SHA256 of the body> so the receiver can check the sender."}>
          <Input type="password" mono value={secret.signing_secret || ""} onChange={setS("signing_secret")} placeholder={keep("signing_secret")} autoComplete="off" />
        </Field>
        {removable("signing_secret", "signing secret")}
      </>}

      <ToggleRow on={enabled} onClick={() => setEnabled(!enabled)} title="Enabled" sub="A disabled channel is skipped by every rule." />
      {test && (
        <div style={{ display: "flex", gap: 8, alignItems: "flex-start", padding: "8px 10px", borderRadius: 6, fontSize: 12, background: test.ok ? "var(--ok-soft)" : "var(--err-soft)" }}>
          <Icon name={test.ok ? "check_circle" : "error"} style={{ fontSize: 16, color: test.ok ? "var(--ok)" : "var(--err)" }} />
          <span>{test.ok ? "Sent: check that it arrived." : test.error}</span>
        </div>
      )}
      {err && <div style={{ display: "flex", gap: 6, alignItems: "center", color: "var(--err)", fontSize: 12 }}><Icon name="error" style={{ fontSize: 16 }} />{err}</div>}
    </Modal>
  );
}
