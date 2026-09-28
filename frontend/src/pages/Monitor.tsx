import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { monitorApi, type CheckOut, type MonitorSnapshot } from "../lib/api";
import { useToast } from "../lib/toast";
import { useTheme } from "../lib/theme";
import { dbTile, fmtEta, fmtN, relativeTime, statusBadge } from "../lib/format";
import { Icon } from "../ui/Icon";
import { Button, IconButton } from "../ui/Button";
import { StatusBadge } from "../ui/Badge";
import { ProgressBar } from "../ui/ProgressBar";
import { ServiceCheckModal } from "./monitor/ServiceCheckModal";

const LEVEL_ICON = { ok: ["check_circle", "var(--ok)"], high: ["warning", "var(--warn)"] } as const;

export function Monitor() {
  const nav = useNavigate();
  const toast = useToast();
  const { theme } = useTheme();
  const [snap, setSnap] = useState<MonitorSnapshot | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const busy = useRef<Set<string>>(new Set());

  const load = () => monitorApi.get().then(setSnap).catch(() => {});

  useEffect(() => {
    load();
    const iv = setInterval(load, 5000);
    return () => clearInterval(iv);
  }, []);

  async function refresh() {
    setRefreshing(true);
    try { setSnap(await monitorApi.refresh()); } catch (e) { toast("err", "Refresh failed", e instanceof Error ? e.message : undefined); }
    finally { setRefreshing(false); }
  }

  async function runNow(c: CheckOut) {
    busy.current.add(c.id);
    setMenuFor(null);
    try { await monitorApi.runCheck(c.id); await load(); }
    catch (e) { toast("err", `${c.name}: run failed`, e instanceof Error ? e.message : undefined); }
    finally { busy.current.delete(c.id); }
  }

  async function toggleEnabled(c: CheckOut) {
    setMenuFor(null);
    try { await monitorApi.updateCheck(c.id, { enabled: !c.enabled }); await load(); }
    catch (e) { toast("err", "Could not update the check", e instanceof Error ? e.message : undefined); }
  }

  async function remove(c: CheckOut) {
    setMenuFor(null);
    if (!confirm(`Delete the check "${c.name}"?`)) return;
    try { await monitorApi.deleteCheck(c.id); await load(); }
    catch (e) { toast("err", "Could not delete the check", e instanceof Error ? e.message : undefined); }
  }

  if (!snap) return null;
  const dark = theme === "dark";
  const bars = [
    { label: "CPU", pct: snap.host.cpu.percent, val: `${snap.host.cpu.percent}%`, level: snap.host.cpu.level, sub: snap.host.cpu.load_avg ? `load avg ${snap.host.cpu.load_avg.join(" · ")}` : "load average unavailable" },
    { label: "Memory", pct: snap.host.memory.percent, val: `${snap.host.memory.used_gb} / ${snap.host.memory.total_gb} GB`, level: snap.host.memory.level, sub: `${snap.host.memory.percent}% used` },
    ...snap.host.disks.map((d) => ({ label: `Disk · ${d.label}`, pct: d.percent, val: `${d.used_gb} / ${d.total_gb} GB`, level: d.level, sub: d.level === "high" ? `above ${snap.host.warn_at_pct}% threshold` : d.path })),
  ];

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1440 }}>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Monitor</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>Current state only — nothing here is stored as history.</p>
        </div>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, height: 32, padding: "0 12px", borderRadius: 6, border: "1px solid var(--border)", background: "var(--surface)", fontSize: 12, color: "var(--text-2)" }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--ok)", animation: "pulse 2s infinite" }} />
          Live · updated {relativeTime(snap.updated_at)}
        </span>
        <Button icon="refresh" disabled={refreshing} onClick={refresh}>{refreshing ? "Refreshing…" : "Refresh"}</Button>
        <Button variant="primary" icon="add" onClick={() => setAddOpen(true)}>Add service check</Button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(480px,1fr))", gap: 16, alignItems: "start" }}>
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
            <Icon name="database" style={{ color: "var(--text-3)" }} /><span style={{ fontWeight: 600, flex: 1 }}>Database connections</span>
            <span style={{ fontSize: 12, color: "var(--text-2)" }}>{snap.summary.connections.up} up · {snap.summary.connections.down} down</span>
          </div>
          {snap.connections.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>No connections saved yet.</div>}
          {snap.connections.map((c) => {
            const tile = dbTile(c.type, dark);
            const b = statusBadge(c.status === "err" ? "down" : c.status === "ok" ? "up" : "pending");
            return (
              <div key={c.id} style={{ display: "grid", gridTemplateColumns: "24px minmax(0,1fr) 140px 70px 104px", gap: 10, alignItems: "center", padding: "0 14px", height: 44, borderBottom: "1px solid var(--border)" }}>
                <span style={{ width: 24, height: 24, borderRadius: 5, background: tile.tbg, color: tile.tfg, fontFamily: "'Geist Mono',monospace", fontSize: 10, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center" }}>{c.type.slice(0, 2).toUpperCase()}</span>
                <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
                <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.server_version || "—"}</span>
                <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)", textAlign: "right" }}>{c.latency_ms != null ? (c.latency_ms < 1 ? "<1 ms" : `${Math.round(c.latency_ms)} ms`) : "—"}</span>
                <span style={{ justifySelf: "end" }}><StatusBadge b={b} size="sm" /></span>
              </div>
            );
          })}
        </div>

        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
            <Icon name="memory" style={{ color: "var(--text-3)" }} /><span style={{ fontWeight: 600, flex: 1 }}>Host resources</span>
            <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)" }}>{snap.host.hostname} · {snap.host.vcpus} vCPU · {snap.host.os}</span>
          </div>
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 16 }}>
            {bars.map((r) => {
              const [icon, c] = LEVEL_ICON[r.level];
              return (
                <div key={r.label} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                    <span style={{ fontWeight: 500, flex: 1 }}>{r.label}</span>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 11, fontWeight: 500, color: c }}><Icon name={icon} style={{ fontSize: 13 }} />{r.level === "high" ? "High" : "OK"}</span>
                    <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)", minWidth: 120, textAlign: "right" }}>{r.val}</span>
                  </div>
                  <ProgressBar pct={r.pct} color={c} height={8} />
                  <span style={{ fontSize: 11, color: "var(--text-3)" }}>{r.sub}</span>
                </div>
              );
            })}
            {snap.host.disks_unavailable.length > 0 && (
              <div style={{ fontSize: 11, color: "var(--text-3)" }}>Not mounted in this container: {snap.host.disks_unavailable.join(", ")}</div>
            )}
          </div>
        </div>

        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
            <Icon name="bolt" style={{ color: "var(--text-3)" }} /><span style={{ fontWeight: 600, flex: 1 }}>Running jobs</span>
            <Button variant="ghost" height={24} padX={8} onClick={() => nav("/jobs")} style={{ color: "var(--accent)", fontSize: 12 }}>Open Jobs</Button>
          </div>
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
              <Stat label="Running" value={String(snap.jobs.running)} />
              <Stat label="Queued" value={String(snap.jobs.queued)} />
              <Stat label="Worker slots" value={snap.jobs.slots_total != null ? `${snap.jobs.running} / ${snap.jobs.slots_total}` : snap.jobs.worker_online === false ? "no worker" : "n/a"} />
            </div>
            {snap.jobs.active.length === 0 && <div style={{ fontSize: 12, color: "var(--text-3)" }}>Nothing running right now.</div>}
            {snap.jobs.active.map((j) => {
              const b = statusBadge(j.status);
              const pct = j.progress != null ? j.progress * 100 : 0;
              return (
                <div key={j.id} style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <span style={{ flex: 1, fontFamily: "'Geist Mono',monospace", fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.name}</span>
                    <StatusBadge b={b} size="sm" />
                  </div>
                  <ProgressBar pct={pct} height={4} />
                  <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)" }}>
                    {j.progress != null ? `${pct.toFixed(1)}%` : "—"} · {j.rate_rows_s ? `${fmtN(j.rate_rows_s)} rows/s` : "—"}{j.eta_s != null ? ` · ~${fmtEta(j.eta_s)} left` : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
            <Icon name="hub" style={{ color: "var(--text-3)" }} /><span style={{ fontWeight: 600, flex: 1 }}>External services</span>
            <span style={{ fontSize: 12, color: "var(--text-2)" }}>{snap.summary.services.up} of {snap.summary.services.total} up</span>
          </div>
          {snap.services.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>No service checks yet.</div>}
          {snap.services.map((s) => {
            const b = statusBadge(s.status);
            return (
              <div key={s.id} style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 58px 120px 90px 28px", gap: 10, alignItems: "center", padding: "8px 14px", borderBottom: "1px solid var(--border)", opacity: s.enabled ? 1 : 0.5, position: "relative" }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 500 }}>{s.name}</div>
                  <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.target}</div>
                </div>
                <span style={{ justifySelf: "start", fontFamily: "'Geist Mono',monospace", fontSize: 10, fontWeight: 500, padding: "1px 6px", borderRadius: 4, border: "1px solid var(--border-2)", color: "var(--text-2)" }}>{s.kind.toUpperCase()}</span>
                <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)", minWidth: 0 }}>
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.message || (s.enabled ? "waiting for first check" : "disabled")}</div>
                  <div style={{ color: "var(--text-3)" }}>{s.latency_ms != null ? `${Math.round(s.latency_ms)} ms · ` : ""}{s.interval_s}s{s.stale ? " · stale" : ""}</div>
                </div>
                <span style={{ justifySelf: "end" }}><StatusBadge b={b} size="sm" /></span>
                <div style={{ justifySelf: "end", position: "relative" }}>
                  <IconButton icon="more_horiz" title="More" onClick={() => setMenuFor(menuFor === s.id ? null : s.id)} size={24} />
                  {menuFor === s.id && (
                    <div style={{ position: "absolute", right: 0, top: 26, width: 150, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, boxShadow: "var(--shadow-2)", padding: 4, zIndex: 5, display: "flex", flexDirection: "column" }}>
                      <MenuBtn icon="bolt" label="Run now" onClick={() => runNow(s)} />
                      <MenuBtn icon={s.enabled ? "pause" : "play_arrow"} label={s.enabled ? "Disable" : "Enable"} onClick={() => toggleEnabled(s)} />
                      <MenuBtn icon="delete" label="Delete" tone="var(--err)" onClick={() => remove(s)} />
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {addOpen && <ServiceCheckModal conns={snap.connections} onClose={() => setAddOpen(false)} onSaved={() => { setAddOpen(false); load(); }} />}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--text-3)" }}>{label}</div>
      <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 22 }}>{value}</div>
    </div>
  );
}

function MenuBtn({ icon, label, onClick, tone }: { icon: string; label: string; onClick: () => void; tone?: string }) {
  return (
    <button onClick={onClick} className="hover-surface2" style={{ height: 30, display: "flex", alignItems: "center", gap: 8, padding: "0 8px", border: "none", background: "transparent", borderRadius: 5, cursor: "pointer", textAlign: "left", color: tone || "var(--text-2)" }}>
      <Icon name={icon} style={{ fontSize: 16 }} />{label}
    </button>
  );
}
