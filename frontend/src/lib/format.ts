// Number/duration formatting and the status-badge lookup, ported from the design prototype's
// `C.fmt` / `C.fmtN` / `C.fmtDur` / `C.fmtEta` / `C.st` helpers (frontend/design/Conduit v2.dc.html).

export function fmt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

export function fmtN(n: number): string {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "k";
  return fmt(n);
}

export function fmtDur(s: number): string {
  if (s < 90) return `${Math.max(1, Math.round(s))}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
}

export function fmtEta(s: number): string {
  if (s >= 60) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.round(s)}s`;
}

export function fmtBytes(n: number): string {
  if (n >= 1e12) return (n / 1e12).toFixed(1) + " TB";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + " KB";
  return `${n} B`;
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "Never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function untilTime(iso: string | null): string {
  if (!iso) return "—";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s <= 0) return "due now";
  if (s < 60) return `in ${Math.ceil(s)}s`;
  if (s < 3600) return `in ${Math.ceil(s / 60)}m`;
  if (s < 86400) return `in ${Math.ceil(s / 3600)}h`;
  return `in ${Math.ceil(s / 86400)}d`;
}

const CRON_LABELS: Record<string, string> = {
  "@hourly": "Hourly", "@daily": "Daily", "@midnight": "Daily", "@weekly": "Weekly", "@monthly": "Monthly", "@yearly": "Yearly", "@annually": "Yearly",
};
export function cronLabel(expr: string): string {
  return CRON_LABELS[expr.trim()] || expr;
}

export function clockUtc(unixSeconds: number): string {
  const d = new Date(unixSeconds * 1000);
  return d.toISOString().slice(11, 19);
}

// ---- status badges -----------------------------------------------------------------
// One lookup for every status word the app shows (connections, jobs, splits, service checks).

export interface Badge { label: string; icon: string; fg: string; bg: string; spin: boolean; }

const STATUS_MAP: Record<string, [string, string, string]> = {
  ok: ["Healthy", "check_circle", "ok"],
  warn: ["Degraded", "warning", "warn"],
  err: ["Down", "error", "err"],
  idle: ["Unknown", "help", "idle"],
  testing: ["Testing", "progress_activity", "accent"],
  queued: ["Queued", "schedule", "idle"],
  planning: ["Planning", "progress_activity", "accent"],
  running: ["Running", "autorenew", "warn"],
  succeeded: ["Succeeded", "check_circle", "ok"],
  failed: ["Failed", "cancel", "err"],
  cancelled: ["Cancelled", "block", "idle"],
  stalled: ["Stalled", "pause_circle", "err"],
  done: ["Done", "check_circle", "ok"],
  pending: ["Pending", "schedule", "idle"],
  never: ["Never run", "remove", "idle"],
  up: ["Up", "check_circle", "ok"],
  down: ["Down", "error", "err"],
};

export function statusBadge(status: string, label?: string): Badge {
  const s = STATUS_MAP[status] || ["Unknown", "help", "idle"];
  const tone = s[2] === "accent" ? "accent" : s[2];
  return {
    label: label || s[0],
    icon: s[1],
    fg: `var(--${tone})`,
    bg: `var(--${tone}-soft)`,
    spin: status === "testing" || status === "running" || status === "planning",
  };
}

// ---- per-database-type color tile (matches C.tile()/DB[type].h in the prototype) --------

const DB_HUE: Record<string, number> = {
  postgres: 255, clickhouse: 85, mysql: 220, sqlite: 195, duckdb: 105, mongodb: 150,
};

export function dbTile(type: string, dark: boolean): { tbg: string; tfg: string } {
  const h = DB_HUE[type] ?? 240;
  return {
    tbg: `oklch(0.65 0.12 ${h} / ${dark ? 0.18 : 0.14})`,
    tfg: `oklch(${dark ? 0.8 : 0.48} 0.13 ${h})`,
  };
}
