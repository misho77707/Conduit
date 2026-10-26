import { useEffect, useState } from "react";
import { sessionEnded, settingsApi, type SettingOut } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { relativeTime } from "../../lib/format";
import { Button } from "../../ui/Button";
import { Icon } from "../../ui/Icon";
import { Input } from "../../ui/Input";
import { Panel } from "./Alerts";

const GROUP_ICON: Record<string, string> = {
  jobs: "bolt", files: "upload_file", combined: "join_inner", monitor: "monitor_heart", alerts: "notifications", system: "settings",
};

function show(v: number | string | null, unit = ""): string {
  if (v === null || v === "") return "(empty)";
  return typeof v === "number" ? `${v.toLocaleString()}${unit ? ` ${unit}` : ""}` : v;
}

function range(s: SettingOut): string {
  if (s.type === "text") return `Default: ${show(s.default)}`;
  return `${show(s.min)} – ${show(s.max)}${s.unit ? ` ${s.unit}` : ""} · default ${show(s.default)}`;
}

function Source({ s }: { s: SettingOut }) {
  const [label, sub, color] =
    s.source === "ui" ? ["Set here", `by ${s.updated_by || "a deleted user"} · ${relativeTime(s.updated_at)}`, "var(--accent)"]
    : s.source === "env" ? [".env", s.env, "var(--text)"]
    : ["Default", s.cls === "restart" ? s.env : "", "var(--text-2)"];
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 12, fontWeight: 500, color }}>{label}</div>
      {sub && <div title={sub} style={{ fontSize: 11, color: "var(--text-3)", fontFamily: s.source === "ui" ? undefined : "'Geist Mono',monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sub}</div>}
    </div>
  );
}

function Row({ s, onChange }: { s: SettingOut; onChange: (s: SettingOut) => void }) {
  const toast = useToast();
  const [draft, setDraft] = useState(String(s.value));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setDraft(String(s.value)); setError(null); }, [s.value]);
  const dirty = draft !== String(s.value);

  const run = async (call: Promise<SettingOut>, done: (r: SettingOut) => string) => {
    setBusy(true);
    try {
      const r = await call;
      onChange(r);
      toast("ok", done(r), "Applies everywhere within 15 seconds: API, workers and schedules.");
    } catch (e) {
      if (!sessionEnded(e)) setError(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  };
  const save = () => run(settingsApi.set(s.key, s.type === "text" ? draft : draft.trim()), (r) => `${r.label}: ${show(r.value, r.unit)}`);
  const reset = () => run(settingsApi.reset(s.key), (r) => `${r.label} back to ${r.source === "env" ? ".env" : "the default"}: ${show(r.value, r.unit)}`);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 300px 170px 92px", gap: 14, alignItems: "start", padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 500, display: "flex", alignItems: "center", gap: 6 }}>
          {s.label}
          {s.cls === "restart" && <span title="Set in .env, then restart the containers" style={{ fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em", padding: "1px 6px", borderRadius: 4, border: "1px solid var(--border-2)", color: "var(--text-3)" }}>restart</span>}
        </div>
        <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 2 }}>{s.help}</div>
        {s.note && <div style={{ fontSize: 12, color: "var(--warn)", marginTop: 4, display: "flex", gap: 4, alignItems: "center" }}><Icon name="warning" style={{ fontSize: 14 }} />{s.note}</div>}
      </div>
      {s.cls === "runtime" ? (
        <div>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <Input value={draft} disabled={busy} inputMode={s.type === "text" ? undefined : "decimal"} placeholder={s.type === "text" ? show(s.default) : undefined}
              onChange={(e) => { setDraft(e.target.value); setError(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" && dirty) save(); if (e.key === "Escape") { setDraft(String(s.value)); setError(null); } }}
              style={{ fontSize: 12, borderColor: error ? "var(--err)" : undefined }} />
            <span style={{ width: 76, flexShrink: 0, fontSize: 12, color: "var(--text-3)", whiteSpace: "nowrap" }}>{s.unit}</span>
          </div>
          <div style={{ fontSize: 11, marginTop: 4, color: error ? "var(--err)" : "var(--text-3)" }}>{error || range(s)}</div>
        </div>
      ) : (
        <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, paddingTop: 7, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={String(s.value)}>{show(s.value, s.unit)}</div>
      )}
      <div style={{ paddingTop: 4 }}><Source s={s} /></div>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        {s.cls === "runtime" && (dirty
          ? <Button variant="primary" height={30} disabled={busy} onClick={save}>Save</Button>
          : s.source === "ui" && <Button variant="ghost" height={30} icon="restart_alt" disabled={busy} onClick={reset}
              title={`Back to ${s.env_value !== null ? `the .env value (${show(s.env_value, s.unit)})` : `the default (${show(s.default, s.unit)})`}`}>Reset</Button>)}
      </div>
    </div>
  );
}

/** Control & Setup > Settings: what admins may change without a restart, and where every value comes from. */
export function SettingsTab() {
  const toast = useToast();
  const [items, setItems] = useState<SettingOut[] | null>(null);

  useEffect(() => {
    settingsApi.list().then(setItems).catch((e) => sessionEnded(e) || toast("err", "Could not load settings", e instanceof Error ? e.message : undefined));
  }, [toast]);

  if (!items) return null;
  const groups = items.reduce<string[]>((acc, s) => (acc.includes(s.group) ? acc : [...acc, s.group]), []);
  const replace = (r: SettingOut) => setItems((xs) => xs && xs.map((x) => (x.key === r.key ? r : x)));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ fontSize: 13, color: "var(--text-2)" }}>
        A value saved here wins over .env and reaches every process within 15 seconds, without a restart.
        Reset goes back to .env or the default. Settings marked <b style={{ fontWeight: 600 }}>restart</b> are set in .env and need the containers restarted.
      </div>
      {groups.map((g) => {
        const rows = items.filter((s) => s.group === g);
        return (
          <Panel key={g} icon={GROUP_ICON[g] || "tune"} title={rows[0].group_label}>
            {rows.map((s) => <Row key={s.key} s={s} onChange={replace} />)}
          </Panel>
        );
      })}
    </div>
  );
}
