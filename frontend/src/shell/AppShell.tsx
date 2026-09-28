import { useEffect, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import { jobsApi } from "../lib/api";
import { Icon } from "../ui/Icon";
import { IconButton } from "../ui/Button";
import { Logo } from "./Logo";

const DATA_NAV = [
  { id: "connections", label: "Connections", icon: "database" },
  { id: "upload", label: "Upload & Load", icon: "upload_file" },
  { id: "jobs", label: "Jobs", icon: "bolt" },
  { id: "pipelines", label: "Pipelines", icon: "account_tree" },
];
const OPS_NAV = [
  { id: "monitor", label: "Monitor", icon: "monitor_heart" },
  { id: "control", label: "Control & Setup", icon: "tune", soon: true },
];
const TITLES: Record<string, string> = {
  connections: "Connections", upload: "Upload & Load", jobs: "Jobs", pipelines: "Pipelines",
  monitor: "Monitor", control: "Control & Setup",
};

export function AppShell({ children }: { children: ReactNode }) {
  const nav = useNavigate();
  const loc = useLocation();
  const page = loc.pathname.split("/")[1] || "connections";
  const { user, logout } = useAuth();
  const { theme, toggle } = useTheme();
  const [collapsed, setCollapsed] = useState(() => window.innerWidth < 1100);
  const [userMenu, setUserMenu] = useState(false);
  const [runningJobs, setRunningJobs] = useState(0);

  useEffect(() => {
    let alive = true;
    const tick = () => jobsApi.list().then((js) => { if (alive) setRunningJobs(js.filter((j) => j.status === "running" || j.status === "planning").length); }).catch(() => {});
    tick();
    const iv = setInterval(tick, 5000);
    return () => { alive = false; clearInterval(iv); };
  }, []);

  const item = (n: { id: string; label: string; icon: string; soon?: boolean }) => {
    const active = page === n.id;
    return (
      <button
        key={n.id}
        title={n.label}
        onClick={() => { nav(`/${n.id}`); setUserMenu(false); }}
        className="hover-surface2"
        style={{
          height: 32, display: "flex", alignItems: "center", gap: 10, padding: "0 9px", borderRadius: 6, border: "none",
          background: active ? "var(--surface-3)" : "transparent", color: active ? "var(--text)" : "var(--text-2)",
          cursor: "pointer", textAlign: "left", whiteSpace: "nowrap", fontWeight: 500, width: "100%",
        }}
      >
        <Icon name={n.icon} style={{ color: active ? "var(--accent)" : "var(--text-3)" }} />
        {!collapsed && <span style={{ flex: 1 }}>{n.label}</span>}
        {!collapsed && n.soon && (
          <span style={{ fontSize: 10, fontWeight: 600, letterSpacing: "0.04em", padding: "1px 6px", borderRadius: 4, background: "var(--accent-soft)", color: "var(--accent)" }}>SOON</span>
        )}
      </button>
    );
  };

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", background: "var(--bg)", color: "var(--text)", overflow: "hidden" }}>
      <header style={{ height: 48, flexShrink: 0, display: "flex", alignItems: "center", gap: 10, padding: "0 12px 0 10px", borderBottom: "1px solid var(--border)", background: "var(--surface)", position: "relative", zIndex: 20 }}>
        <IconButton icon={collapsed ? "left_panel_open" : "left_panel_close"} title="Toggle sidebar" onClick={() => setCollapsed((c) => !c)} size={32} />
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <Logo size={22} />
          <span style={{ fontWeight: 600, fontSize: 14, letterSpacing: "-0.01em" }}>Conduit</span>
          <span style={{ color: "var(--text-3)" }}>/</span>
          <span style={{ color: "var(--text-2)" }}>{TITLES[page]}</span>
        </div>
        <div style={{ flex: 1 }} />
        {runningJobs > 0 && (
          <button
            onClick={() => nav("/jobs")}
            style={{ height: 28, display: "flex", alignItems: "center", gap: 6, padding: "0 10px", borderRadius: 14, border: "1px solid var(--border)", background: "var(--warn-soft)", color: "var(--warn)", fontSize: 12, fontWeight: 500, cursor: "pointer" }}
          >
            <Icon name="autorenew" spin style={{ fontSize: 15 }} />
            {runningJobs === 1 ? "1 job running" : `${runningJobs} jobs running`}
          </button>
        )}
        <IconButton icon={theme === "dark" ? "light_mode" : "dark_mode"} title="Toggle light / dark" onClick={toggle} size={32} />
        <div style={{ position: "relative" }}>
          <button
            onClick={() => setUserMenu((v) => !v)}
            className="hover-surface2"
            style={{ height: 32, display: "flex", alignItems: "center", gap: 6, padding: "0 6px 0 4px", borderRadius: 6, border: "none", background: "transparent", cursor: "pointer" }}
          >
            <span style={{ width: 24, height: 24, borderRadius: "50%", background: "var(--surface-3)", border: "1px solid var(--border-2)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 600 }}>
              {(user?.username || "?").slice(0, 2).toUpperCase()}
            </span>
            <Icon name="expand_more" style={{ fontSize: 16, color: "var(--text-3)" }} />
          </button>
          {userMenu && (
            <div style={{ position: "absolute", right: 0, top: 38, width: 220, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, boxShadow: "var(--shadow-2)", padding: 4, display: "flex", flexDirection: "column" }}>
              <div style={{ padding: "8px 10px 10px", borderBottom: "1px solid var(--border)", marginBottom: 4 }}>
                <div style={{ fontWeight: 500 }}>{user?.username}</div>
                <div style={{ fontSize: 12, color: "var(--text-3)" }}>Local account · full access</div>
              </div>
              <MenuButton icon={theme === "dark" ? "light_mode" : "dark_mode"} label="Switch theme" onClick={toggle} />
              <MenuButton icon="keyboard" label="Keyboard shortcuts" />
              <div style={{ height: 1, background: "var(--border)", margin: "4px 0" }} />
              <MenuButton icon="logout" label="Sign out" onClick={logout} />
            </div>
          )}
        </div>
      </header>
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <nav style={{ width: collapsed ? 52 : 224, flexShrink: 0, borderRight: "1px solid var(--border)", background: "var(--surface)", display: "flex", flexDirection: "column", padding: "10px 8px", gap: 2, transition: "width .15s", overflow: "hidden" }}>
          {!collapsed && <NavLabel>Data</NavLabel>}
          {DATA_NAV.map(item)}
          {!collapsed && <NavLabel top>Operate</NavLabel>}
          {collapsed && <div style={{ height: 1, background: "var(--border)", margin: "8px 4px" }} />}
          {OPS_NAV.map(item)}
          <div style={{ flex: 1 }} />
          {!collapsed && <div style={{ padding: "8px 9px 2px", fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)", whiteSpace: "nowrap" }}>local</div>}
        </nav>
        <main style={{ flex: 1, minWidth: 0, overflow: "auto" }}>{children}</main>
      </div>
    </div>
  );
}

function NavLabel({ children, top }: { children: ReactNode; top?: boolean }) {
  return <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-3)", padding: top ? "14px 8px 4px" : "6px 8px 4px", letterSpacing: "0.04em", textTransform: "uppercase" }}>{children}</div>;
}

function MenuButton({ icon, label, onClick }: { icon: string; label: string; onClick?: () => void }) {
  return (
    <button
      onClick={onClick}
      className="hover-surface2"
      style={{ height: 30, display: "flex", alignItems: "center", gap: 8, padding: "0 8px", border: "none", background: "transparent", borderRadius: 5, cursor: "pointer", textAlign: "left", color: "var(--text-2)" }}
    >
      <Icon name={icon} style={{ fontSize: 17 }} />
      {label}
    </button>
  );
}
