import { useEffect, useMemo, useState } from "react";
import {
  alertsApi, connectionsApi, monitorApi, pipelinesApi, type AlertChannelOut, type AlertEvent, type AlertRuleOut,
  type AlertScope, type AlertSendResult,
} from "../../lib/api";
import { useToast } from "../../lib/toast";
import { Modal } from "../../ui/Modal";
import { Field } from "../../ui/Field";
import { Input, Select } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { Icon } from "../../ui/Icon";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ToggleRow } from "../../ui/Toggle";
import { EVENTS, EVENT_ORDER, KIND, describeRule, type Names, type ScopeKey } from "./alertText";

interface Pick { id: string; label: string; }

function Checklist({ items, value, onChange, empty }: { items: Pick[]; value: string[]; onChange: (v: string[]) => void; empty: string }) {
  if (!items.length) return <div style={{ fontSize: 12, color: "var(--text-3)" }}>{empty}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, maxHeight: 170, overflow: "auto", border: "1px solid var(--border)", borderRadius: 6, padding: 6 }}>
      {items.map((it) => (
        <label key={it.id} className="hover-surface2" style={{ display: "flex", gap: 8, alignItems: "center", padding: "3px 6px", borderRadius: 4, cursor: "pointer" }}>
          <input type="checkbox" checked={value.includes(it.id)} onChange={(e) => onChange(e.target.checked ? [...value, it.id] : value.filter((x) => x !== it.id))} />
          <span>{it.label}</span>
        </label>
      ))}
    </div>
  );
}

export function RuleModal({ rule, channels, onClose, onSaved }: { rule?: AlertRuleOut; channels: AlertChannelOut[]; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(rule?.name || "");
  const [event, setEvent] = useState<AlertEvent>(rule?.event || "job_failed");
  const [options, setOptions] = useState<Record<string, any>>(rule ? { ...rule.options } : { ...EVENTS.job_failed.defaults });
  const [scope, setScope] = useState<AlertScope>(rule?.scope || { all: true });
  const [chans, setChans] = useState<string[]>(rule?.channel_ids || []);
  const [enabled, setEnabled] = useState(rule?.enabled ?? true);
  const [lists, setLists] = useState<{ pipelines: Pick[]; connections: Pick[]; envs: Pick[]; checks: Pick[] }>({ pipelines: [], connections: [], envs: [], checks: [] });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [tested, setTested] = useState<AlertSendResult[] | null>(null);

  useEffect(() => {
    pipelinesApi.list().then((ps) => setLists((l) => ({ ...l, pipelines: ps.map((p) => ({ id: p.id, label: p.name })) }))).catch(() => {});
    connectionsApi.list().then((cs) => setLists((l) => ({
      ...l, connections: cs.map((c) => ({ id: c.id, label: c.name })),
      envs: [...new Set(cs.map((c) => c.env).filter((e): e is string => !!e))].sort().map((e) => ({ id: e, label: e })),
    }))).catch(() => {});
    monitorApi.listChecks().then((ks) => setLists((l) => ({ ...l, checks: ks.map((k) => ({ id: k.id, label: k.name })) }))).catch(() => {});
  }, []);

  const names: Names = useMemo(() => ({
    channels: new Map(channels.map((c) => [c.id, c.name])),
    pipelines: new Map(lists.pipelines.map((p) => [p.id, p.label])),
    connections: new Map(lists.connections.map((p) => [p.id, p.label])),
    checks: new Map(lists.checks.map((p) => [p.id, p.label])),
  }), [channels, lists]);

  const ev = EVENTS[event];
  const opt = (k: string, v: unknown) => setOptions((o) => ({ ...o, [k]: v }));
  const picking = !scope.all;
  const draft = { event, scope, options, channel_ids: chans };

  function pickEvent(e: AlertEvent) {
    setEvent(e);
    setOptions({ ...EVENTS[e].defaults });
    setScope({ all: true });
  }

  async function save() {
    setErr("");
    if (!name.trim()) return setErr("Give the rule a name.");
    setBusy(true);
    try {
      const body = { name: name.trim(), event, scope, options, channel_ids: chans, enabled };
      if (rule) await alertsApi.updateRule(rule.id, body);
      else await alertsApi.createRule(body);
      toast("ok", rule ? `Saved ${body.name}` : `Added ${body.name}`);
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  async function sendTest() {
    setErr("");
    setTested(null);
    if (!chans.length) return setErr("Pick a channel to test.");
    setBusy(true);
    try { setTested(await alertsApi.sendTest(chans, name.trim() || undefined)); }
    catch (e) { setErr(e instanceof Error ? e.message : "Could not send"); }
    finally { setBusy(false); }
  }

  const scopeList = (key: ScopeKey, items: Pick[], empty: string, label: string) => (
    <Field label={label}>
      <Checklist items={items} value={(scope as any)[key] || []} empty={empty}
        onChange={(v) => setScope((s) => ({ ...s, all: false, [key]: v }))} />
    </Field>
  );
  const num = (k: string, label: string, hint?: string, placeholder?: string) => (
    <Field label={label} hint={hint}>
      <Input mono value={options[k] ?? ""} placeholder={placeholder} onChange={(e) => opt(k, e.target.value === "" ? null : Number(e.target.value))} />
    </Field>
  );

  return (
    <Modal width={620} title={rule ? `Edit ${rule.name}` : "Add rule"} sub="When something happens, which channels hear about it. One message when it breaks, one when it recovers." onClose={onClose}
      footer={<>
        <Button icon="send" disabled={busy} onClick={sendTest}>Send test</Button>
        <div style={{ flex: 1 }} />
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={busy} onClick={save}>{busy ? "Working…" : rule ? "Save" : "Add rule"}</Button>
      </>}>
      <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Failed scheduled pipelines" /></Field>
      <Field label="When" hint={ev.help}>
        <Select value={event} onChange={(e) => pickEvent(e.target.value as AlertEvent)}>
          {EVENT_ORDER.map((e) => <option key={e} value={e}>{EVENTS[e].label}</option>)}
        </Select>
      </Field>

      {event === "job_failed" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Which jobs</span>
          <SegmentedControl small value={options.jobs || "all"} onChange={(v) => opt("jobs", v)}
            options={[{ id: "scheduled", label: "Scheduled pipeline runs" }, { id: "pipelines", label: "All pipeline runs" }, { id: "all", label: "All jobs" }]} />
        </div>
      )}
      {event === "input_stale" && num("hours", "No new file for (hours)", "0.5 to 720.")}
      {(event === "connection_down" || event === "check_down") && num("k", "Consecutive failed checks", "1 to 20. One failed check is often a blip.")}
      {event === "connection_down" && <ToggleRow on={!!options.include_warn} onClick={() => opt("include_warn", !options.include_warn)} title="Count degraded (warn) as failed" sub="Slow handshakes count towards the streak too." />}
      {event === "host_high" && <>
        <Field label="Resources">
          <div style={{ display: "flex", gap: 14 }}>
            {["disk", "memory", "cpu"].map((r) => (
              <label key={r} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input type="checkbox" checked={(options.resources || []).includes(r)}
                  onChange={(e) => opt("resources", e.target.checked ? [...(options.resources || []), r] : (options.resources || []).filter((x: string) => x !== r))} />
                {r === "cpu" ? "CPU" : r[0].toUpperCase() + r.slice(1)}
              </label>
            ))}
          </div>
        </Field>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          {num("pct", "Above (%)", "Empty: the Monitor's warning level.", "Monitor level")}
          {num("minutes", "For at least (minutes)", "0 to 1440.")}
        </div>
      </>}

      {ev.scope.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Applies to</span>
          <SegmentedControl small value={picking ? "pick" : "all"} onChange={(v) => setScope(v === "all" ? { all: true } : { all: false })}
            options={[{ id: "all", label: `Every ${ev.scope[0].replace(/s$/, "")}` }, { id: "pick", label: "Only some" }]} />
          {picking && <>
            {ev.scope.includes("pipelines") && scopeList("pipelines", lists.pipelines, "No pipelines yet.", "Pipelines")}
            {ev.scope.includes("connections") && scopeList("connections", lists.connections, "No connections yet.", "Connections")}
            {ev.scope.includes("envs") && lists.envs.length > 0 && scopeList("envs", lists.envs, "", "…or every connection tagged")}
            {ev.scope.includes("checks") && scopeList("checks", lists.checks, "No service checks yet.", "Service checks")}
          </>}
        </div>
      )}

      <Field label="Send to">
        <Checklist items={channels.map((c) => ({ id: c.id, label: `${c.name} · ${KIND[c.kind].label}${c.enabled ? "" : " (disabled)"}` }))}
          value={chans} onChange={setChans} empty="No channels yet: add one under Channels first." />
      </Field>
      <ToggleRow on={enabled} onClick={() => setEnabled(!enabled)} title="Enabled" sub="A disabled rule forgets what it was tracking, without a recovery message." />

      <div style={{ display: "flex", gap: 8, padding: "10px 12px", borderRadius: 6, background: "var(--surface-2)", border: "1px solid var(--border)", fontSize: 13 }}>
        <Icon name="notifications" style={{ fontSize: 17, color: "var(--accent)" }} />
        <span>{describeRule(draft, names)}</span>
      </div>
      {tested && tested.map((t) => (
        <div key={t.channel_id || t.name} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 12 }}>
          <Icon name={t.ok ? "check_circle" : "error"} style={{ fontSize: 16, color: t.ok ? "var(--ok)" : "var(--err)" }} />
          <span><b style={{ fontWeight: 500 }}>{t.name}</b>: {t.ok ? "sent" : t.error}</span>
        </div>
      ))}
      {err && <div style={{ display: "flex", gap: 6, alignItems: "center", color: "var(--err)", fontSize: 12 }}><Icon name="error" style={{ fontSize: 16 }} />{err}</div>}
    </Modal>
  );
}
