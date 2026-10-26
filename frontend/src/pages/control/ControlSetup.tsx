import { Navigate, useNavigate, useParams } from "react-router-dom";
import { useCan, type Can } from "../../lib/auth";
import { Icon } from "../../ui/Icon";
import { AlertsTab } from "./Alerts";
import { Users } from "../Users";

/** Control & Setup: configuring Conduit itself (admins; Users for superadmins). Never starts or stops other tools. */
interface Tab { id: string; label: string; icon: string; need: keyof Can; render: () => JSX.Element; }

const TABS: Tab[] = [
  { id: "alerts", label: "Alerts", icon: "notifications", need: "edit", render: () => <AlertsTab /> },
  { id: "users", label: "Users", icon: "group", need: "manageUsers", render: () => <Users embedded /> },
];

export function ControlSetup() {
  const { tab } = useParams<{ tab: string }>();
  const nav = useNavigate();
  const can = useCan();
  const tabs = TABS.filter((t) => can[t.need]);
  const current = tabs.find((t) => t.id === tab);
  if (!current) return <Navigate to={`/control/${tabs[0]?.id || "alerts"}`} replace />;

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1280 }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Control &amp; Setup</h1>
        <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>How Conduit itself behaves: who hears about problems, and how.</p>
      </div>
      <div role="tablist" style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--border)" }}>
        {tabs.map((t) => {
          const on = t.id === current.id;
          return (
            <button key={t.id} role="tab" aria-selected={on} onClick={() => nav(`/control/${t.id}`)}
              style={{ display: "inline-flex", alignItems: "center", gap: 6, height: 36, padding: "0 12px", border: "none", background: "transparent", cursor: "pointer",
                borderBottom: `2px solid ${on ? "var(--accent)" : "transparent"}`, color: on ? "var(--text)" : "var(--text-2)", fontWeight: 500, marginBottom: -1 }}>
              <Icon name={t.icon} style={{ fontSize: 17, color: on ? "var(--accent)" : "var(--text-3)" }} />{t.label}
            </button>
          );
        })}
      </div>
      {current.render()}
    </div>
  );
}
