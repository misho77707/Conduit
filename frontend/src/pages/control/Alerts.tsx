import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  alertsApi, connectionsApi, monitorApi, pipelinesApi, sessionEnded, type AlertChannelOut, type AlertRuleOut,
} from "../../lib/api";
import { useToast } from "../../lib/toast";
import { relativeTime } from "../../lib/format";
import { Button } from "../../ui/Button";
import { Icon } from "../../ui/Icon";
import { RowMenu } from "../../ui/RowMenu";
import { ChannelModal } from "./ChannelModal";
import { RuleModal } from "./RuleModal";
import { KIND, channelTarget, describeRule, type Names } from "./alertText";

/** Local time "tomorrow 08:00", for "mute until tomorrow". */
function tomorrowMorning(): Date {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(8, 0, 0, 0);
  return d;
}

function clock(iso: string): string {
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return d.toLocaleString([], sameDay ? { hour: "2-digit", minute: "2-digit" } : { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function Panel({ icon, title, sub, right, children }: { icon: string; title: string; sub?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
        <Icon name={icon} style={{ color: "var(--text-3)" }} />
        <span style={{ fontWeight: 600 }}>{title}</span>
        {sub && <span style={{ fontSize: 12, color: "var(--text-3)" }}>{sub}</span>}
        <div style={{ flex: 1 }} />
        {right}
      </div>
      {children}
    </div>
  );
}

export function AlertsTab() {
  const toast = useToast();
  const [channels, setChannels] = useState<AlertChannelOut[] | null>(null);
  const [rules, setRules] = useState<AlertRuleOut[] | null>(null);
  const [muted, setMuted] = useState<string | null>(null);
  const [names, setNames] = useState<Omit<Names, "channels">>({ pipelines: new Map(), connections: new Map(), checks: new Map() });
  const [editChannel, setEditChannel] = useState<AlertChannelOut | "new" | null>(null);
  const [editRule, setEditRule] = useState<AlertRuleOut | "new" | null>(null);

  const fail = (what: string) => (e: unknown) => sessionEnded(e) || toast("err", what, e instanceof Error ? e.message : undefined);
  const load = () => {
    alertsApi.channels().then(setChannels).catch(fail("Could not load channels"));
    alertsApi.rules().then(setRules).catch(fail("Could not load rules"));
    alertsApi.mute().then((m) => setMuted(m.until)).catch(() => {});
  };
  useEffect(() => {
    load();
    pipelinesApi.list().then((ps) => setNames((n) => ({ ...n, pipelines: new Map(ps.map((p) => [p.id, p.name])) }))).catch(() => {});
    connectionsApi.list().then((cs) => setNames((n) => ({ ...n, connections: new Map(cs.map((c) => [c.id, c.name])) }))).catch(() => {});
    monitorApi.listChecks().then((ks) => setNames((n) => ({ ...n, checks: new Map(ks.map((k) => [k.id, k.name])) }))).catch(() => {});
  }, []);

  const allNames: Names = useMemo(() => ({ ...names, channels: new Map((channels || []).map((c) => [c.id, c.name])) }), [names, channels]);

  async function mute(until: Date | null) {
    try {
      const r = await alertsApi.setMute(until ? until.toISOString() : null);
      setMuted(r.until);
      toast("ok", r.until ? `Alerts muted until ${clock(r.until)}` : "Alerts unmuted", r.until ? "Nothing is sent meanwhile; one summary when the mute ends." : "A summary of what is still firing goes out now.");
    } catch (e) { fail("Could not change the mute")(e); }
  }
  async function testChannel(c: AlertChannelOut) {
    try {
      const r = await alertsApi.testChannel(c.id);
      toast(r.ok ? "ok" : "err", r.ok ? `Test sent to ${c.name}` : `${c.name}: test failed`, r.error || undefined);
      load();
    } catch (e) { fail("Could not send the test")(e); }
  }
  async function toggleChannel(c: AlertChannelOut) {
    try { await alertsApi.updateChannel(c.id, { enabled: !c.enabled }); load(); } catch (e) { fail("Could not change the channel")(e); }
  }
  async function removeChannel(c: AlertChannelOut) {
    if (!confirm(`Delete the channel ${c.name}?${c.rules.length ? `\n\nRules sending here (${c.rules.join(", ")}) will stop sending to it.` : ""}`)) return;
    try { await alertsApi.removeChannel(c.id); load(); } catch (e) { fail("Could not delete the channel")(e); }
  }
  async function testRule(r: AlertRuleOut) {
    if (!r.channel_ids.length) return toast("warn", `${r.name} sends nowhere yet`, "Edit it and pick a channel.");
    try {
      const res = await alertsApi.sendTest(r.channel_ids, r.name);
      const bad = res.filter((x) => !x.ok);
      toast(bad.length ? "err" : "ok", bad.length ? `${bad.length} of ${res.length} channels failed` : `Test sent to ${res.length} channel${res.length === 1 ? "" : "s"}`,
        bad.map((x) => `${x.name}: ${x.error}`).join(" · ") || undefined);
      load();
    } catch (e) { fail("Could not send the test")(e); }
  }
  async function toggleRule(r: AlertRuleOut) {
    try { await alertsApi.updateRule(r.id, { enabled: !r.enabled }); load(); } catch (e) { fail("Could not change the rule")(e); }
  }
  async function removeRule(r: AlertRuleOut) {
    if (!confirm(`Delete the rule ${r.name}?`)) return;
    try { await alertsApi.removeRule(r.id); load(); } catch (e) { fail("Could not delete the rule")(e); }
  }

  if (!channels || !rules) return null;
  const mutedNow = muted && new Date(muted) > new Date();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "10px 14px", borderRadius: 8,
        border: `1px solid ${mutedNow ? "var(--warn)" : "var(--border)"}`, background: mutedNow ? "var(--warn-soft)" : "var(--surface)" }}>
        <Icon name={mutedNow ? "notifications_off" : "notifications_active"} style={{ color: mutedNow ? "var(--warn)" : "var(--ok)" }} />
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontWeight: 500 }}>{mutedNow ? `Alerts muted until ${clock(muted!)}` : "Alerts are on"}</div>
          <div style={{ fontSize: 12, color: "var(--text-2)" }}>
            {mutedNow ? "Nothing is sent; state keeps updating. When the mute ends, one summary of what is still firing goes out."
              : "Mute everything for maintenance: nothing is sent meanwhile, one summary when it ends."}
          </div>
        </div>
        {mutedNow ? <Button icon="notifications_active" onClick={() => mute(null)}>Unmute now</Button> : <>
          <Button height={30} onClick={() => mute(new Date(Date.now() + 3600e3))}>Mute 1 h</Button>
          <Button height={30} onClick={() => mute(new Date(Date.now() + 4 * 3600e3))}>4 h</Button>
          <Button height={30} onClick={() => mute(tomorrowMorning())}>Until tomorrow 08:00</Button>
        </>}
      </div>

      <Panel icon="campaign" title="Channels" sub="where messages go" right={<Button variant="primary" icon="add" height={30} onClick={() => setEditChannel("new")}>Add channel</Button>}>
        {channels.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>No channels yet. Add Telegram, Slack, an email address or a webhook.</div>}
        {channels.map((c) => (
          <div key={c.id} style={{ display: "grid", gridTemplateColumns: "28px minmax(160px,1fr) minmax(200px,1.4fr) minmax(200px,1.2fr) 32px", gap: 12, alignItems: "center", padding: "10px 14px", borderBottom: "1px solid var(--border)", opacity: c.enabled ? 1 : 0.55 }}>
            <Icon name={KIND[c.kind].icon} style={{ color: "var(--text-3)" }} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 500 }}>{c.name}{!c.enabled && <span style={{ fontSize: 11, color: "var(--text-3)" }}> · disabled</span>}</div>
              <div style={{ fontSize: 11, color: "var(--text-3)" }}>{KIND[c.kind].label}{c.rules.length ? ` · ${c.rules.length} rule${c.rules.length === 1 ? "" : "s"}` : " · no rules"}</div>
            </div>
            <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{channelTarget(c)}</span>
            <div style={{ fontSize: 12, minWidth: 0 }}>
              {c.last_error ? (
                <span title={c.last_error} style={{ display: "flex", gap: 5, alignItems: "center", color: "var(--err)" }}>
                  <Icon name="error" style={{ fontSize: 15, flexShrink: 0 }} />
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.last_error}</span>
                </span>
              ) : c.last_sent_at ? (
                <span style={{ display: "flex", gap: 5, alignItems: "center", color: "var(--text-2)" }}><Icon name="check_circle" style={{ fontSize: 15, color: "var(--ok)" }} />sent {relativeTime(c.last_sent_at)}</span>
              ) : <span style={{ color: "var(--text-3)" }}>nothing sent yet</span>}
            </div>
            <RowMenu width={170} items={[
              { icon: "send", label: "Send test", onClick: () => testChannel(c) },
              { icon: "edit", label: "Edit", onClick: () => setEditChannel(c) },
              { icon: c.enabled ? "pause" : "play_arrow", label: c.enabled ? "Disable" : "Enable", onClick: () => toggleChannel(c) },
              { icon: "delete", label: "Delete", tone: "var(--err)", onClick: () => removeChannel(c) },
            ]} />
          </div>
        ))}
      </Panel>

      <Panel icon="rule" title="Rules" sub="shared by everyone: one message when something breaks, one when it recovers"
        right={<Button variant="primary" icon="add" height={30} onClick={() => setEditRule("new")}>Add rule</Button>}>
        {rules.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>No rules yet. A rule picks an event (a failed pipeline, a connection down …) and the channels to tell.</div>}
        {rules.map((r) => (
          <div key={r.id} style={{ display: "grid", gridTemplateColumns: "minmax(180px,0.8fr) minmax(280px,2fr) 110px 32px", gap: 12, alignItems: "center", padding: "10px 14px", borderBottom: "1px solid var(--border)", opacity: r.enabled ? 1 : 0.55 }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 500 }}>{r.name}</div>
              <div style={{ fontSize: 11, color: "var(--text-3)" }}>{r.event_label}{r.enabled ? "" : " · disabled"}</div>
            </div>
            <span style={{ fontSize: 12, color: "var(--text-2)" }}>{describeRule(r, allNames)}</span>
            <span style={{ fontSize: 12, color: r.firing ? "var(--err)" : "var(--text-3)", display: "flex", gap: 5, alignItems: "center" }}>
              {r.firing ? <><Icon name="notifications_active" style={{ fontSize: 15 }} />{r.firing} firing</> : "quiet"}
            </span>
            <RowMenu width={170} items={[
              { icon: "edit", label: "Edit", onClick: () => setEditRule(r) },
              { icon: "send", label: "Send test", onClick: () => testRule(r) },
              { icon: r.enabled ? "pause" : "play_arrow", label: r.enabled ? "Disable" : "Enable", onClick: () => toggleRule(r) },
              { icon: "delete", label: "Delete", tone: "var(--err)", onClick: () => removeRule(r) },
            ]} />
          </div>
        ))}
      </Panel>

      {editChannel && <ChannelModal channel={editChannel === "new" ? undefined : editChannel} onClose={() => setEditChannel(null)} onSaved={() => { setEditChannel(null); load(); }} />}
      {editRule && <RuleModal rule={editRule === "new" ? undefined : editRule} channels={channels} onClose={() => setEditRule(null)} onSaved={() => { setEditRule(null); load(); }} />}
    </div>
  );
}
