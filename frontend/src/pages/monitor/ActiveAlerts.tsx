import { useEffect, useState } from "react";
import { alertsApi, type ActiveAlertsOut } from "../../lib/api";
import { Icon } from "../../ui/Icon";

function since(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const today = d.toDateString() === new Date().toDateString();
  return d.toLocaleString([], today ? { hour: "2-digit", minute: "2-digit" } : { weekday: "short", hour: "2-digit", minute: "2-digit" });
}
/** What is firing right now (every role: viewers see the same list without ids). Refreshed with the Monitor. */
export function ActiveAlerts({ tick }: { tick: number }) {
  const [data, setData] = useState<ActiveAlertsOut | null>(null);

  useEffect(() => { alertsApi.active().then(setData).catch(() => {}); }, [tick]);

  if (!data) return null;
  const muted = data.muted_until && new Date(data.muted_until) > new Date();
  const n = data.alerts.length;
  return (
    <div style={{ background: "var(--surface)", border: `1px solid ${n ? "var(--err)" : "var(--border)"}`, borderRadius: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: n || muted ? "1px solid var(--border)" : "none" }}>
        <Icon name={n ? "notifications_active" : "notifications"} style={{ color: n ? "var(--err)" : "var(--text-3)" }} />
        <span style={{ fontWeight: 600, flex: 1 }}>Active alerts</span>
        <span style={{ fontSize: 12, color: n ? "var(--err)" : "var(--text-2)" }}>{n ? `${n} firing` : "nothing firing"}</span>
      </div>
      {muted && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", padding: "8px 14px", fontSize: 12, background: "var(--warn-soft)", color: "var(--text)" }}>
          <Icon name="notifications_off" style={{ fontSize: 15, color: "var(--warn)" }} />
          Alerts are muted until {new Date(data.muted_until!).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}: nothing is sent.
        </div>
      )}
      {data.alerts.map((a, i) => (
        <a key={i} href={a.link || "#/monitor"} className="hover-surface2"
          style={{ display: "grid", gridTemplateColumns: "150px minmax(0,1fr) 110px", gap: 12, alignItems: "center", padding: "8px 14px", borderBottom: "1px solid var(--border)", color: "inherit", textDecoration: "none" }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: "var(--err)", textTransform: "uppercase", letterSpacing: "0.03em" }}>{a.event_label}</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.subject_label}</div>
            {a.reason && <div title={a.reason} style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.reason}</div>}
          </div>
          <span title={a.since || ""} style={{ fontSize: 12, color: "var(--text-2)", textAlign: "right" }}>since {since(a.since)}</span>
        </a>
      ))}
    </div>
  );
}
