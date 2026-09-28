import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { connectionsApi, metaApi, type ConnectionOut, type DbTypeOut } from "../lib/api";
import { useTheme } from "../lib/theme";
import { useToast } from "../lib/toast";
import { dbTile, relativeTime, statusBadge } from "../lib/format";
import { Button, IconButton } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Input } from "../ui/Input";
import { StatusBadge } from "../ui/Badge";
import { AddConnectionModal } from "./AddConnectionModal";

type Filter = "all" | "ok" | "warn" | "err" | "idle";

export function Connections() {
  const nav = useNavigate();
  const toast = useToast();
  const { theme } = useTheme();
  const [conns, setConns] = useState<ConnectionOut[] | null>(null);
  const [types, setTypes] = useState<Record<string, DbTypeOut>>({});
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const [testing, setTesting] = useState<Set<string>>(new Set());
  const [addOpen, setAddOpen] = useState(false);
  const [editing, setEditing] = useState<ConnectionOut | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);

  const load = () => connectionsApi.list().then(setConns).catch(() => toast("err", "Could not load connections"));

  useEffect(() => {
    load();
    metaApi.dbTypes().then((ts) => setTypes(Object.fromEntries(ts.map((t) => [t.type, t]))));
  }, []);

  const rows = useMemo(() => {
    if (!conns) return [];
    const ql = q.toLowerCase();
    return conns.filter((c) => (filter === "all" || c.status === filter) && (!ql || (c.name + c.host).toLowerCase().includes(ql)));
  }, [conns, filter, q]);

  const counts = (k: Filter) => (conns ? conns.filter((c) => k === "all" || c.status === k).length : 0);

  async function test(id: string) {
    setTesting((s) => new Set(s).add(id));
    try {
      const r = await connectionsApi.test(id);
      if (!r.ok) toast("err", `${r.connection?.name || "Connection"}: ${r.error?.slice(0, 60) || "test failed"}`, r.error || undefined);
      await load();
    } finally {
      setTesting((s) => { const n = new Set(s); n.delete(id); return n; });
    }
  }

  async function testAll() {
    if (!conns) return;
    conns.forEach((c) => test(c.id));
  }

  async function remove(id: string, name: string) {
    if (!confirm(`Delete connection "${name}"? This cannot be undone.`)) return;
    try {
      await connectionsApi.remove(id);
      toast("ok", `Deleted ${name}`);
      load();
    } catch (e) {
      toast("err", "Could not delete connection", e instanceof Error ? e.message : undefined);
    }
  }

  if (!conns) return null;
  const n = conns.length, healthy = counts("ok");

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1440 }}>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Connections</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>{n ? `${n} saved · ${healthy} healthy · ${n - healthy} need attention` : "Saved database targets and sources."}</p>
        </div>
        {n > 0 && <Button icon="network_check" onClick={testAll}>Test all</Button>}
        <Button variant="primary" icon="add" onClick={() => setAddOpen(true)}>Add connection</Button>
      </div>

      {n === 0 && (
        <div style={{ background: "var(--surface)", border: "1px dashed var(--border-2)", borderRadius: 10, padding: "64px 24px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", gap: 14 }}>
          <span style={{ width: 48, height: 48, borderRadius: 12, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Icon name="database" style={{ fontSize: 26 }} />
          </span>
          <div>
            <div style={{ fontSize: 16, fontWeight: 600 }}>Connect your first database</div>
            <p style={{ margin: "6px auto 0", maxWidth: 420, color: "var(--text-2)" }}>Conduit stores credentials encrypted on this machine and checks each connection in the background. You&rsquo;ll pick one as the target when loading data.</p>
          </div>
          <Button variant="primary" icon="add" onClick={() => setAddOpen(true)}>Add connection</Button>
          <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap", justifyContent: "center" }}>
            {Object.values(types).map((t) => { const tile = dbTile(t.type, theme === "dark"); return (
              <span key={t.type} title={t.label} style={{ width: 26, height: 26, borderRadius: 6, background: tile.tbg, color: tile.tfg, fontFamily: "'Geist Mono',monospace", fontSize: 10, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center" }}>{t.abbr}</span>
            ); })}
          </div>
        </div>
      )}

      {n > 0 && (
        <>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            {([["all", "All"], ["ok", "Healthy"], ["warn", "Degraded"], ["err", "Down"], ["idle", "Unknown"]] as [Filter, string][]).map(([k, l]) => {
              const active = filter === k;
              return (
                <button key={k} onClick={() => setFilter(k)} style={{ height: 28, display: "flex", alignItems: "center", gap: 6, padding: "0 10px", borderRadius: 14, border: `1px solid ${active ? "var(--border-2)" : "var(--border)"}`, background: active ? "var(--surface-3)" : "var(--surface)", color: active ? "var(--text)" : "var(--text-2)", fontSize: 12, fontWeight: 500, cursor: "pointer" }}>
                  <span style={{ width: 7, height: 7, borderRadius: "50%", background: k === "all" ? "var(--text-3)" : `var(--${k})` }} />
                  {l}
                  <span style={{ fontFamily: "'Geist Mono',monospace", color: "var(--text-3)" }}>{counts(k)}</span>
                </button>
              );
            })}
            <div style={{ flex: 1 }} />
            <div style={{ width: 260, height: 30, display: "flex", alignItems: "center", gap: 6, padding: "0 10px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", color: "var(--text-3)" }}>
              <Icon name="filter_list" style={{ fontSize: 16 }} />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by name or host" style={{ flex: 1, minWidth: 0, border: "none", background: "transparent", outline: "none", color: "var(--text)" }} />
            </div>
          </div>

          <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}>
            <div style={{ minWidth: 1040 }}>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(210px,1.3fr) 130px minmax(220px,1.6fr) 124px 84px 100px 128px", gap: 12, alignItems: "center", padding: "0 16px", height: 36, borderBottom: "1px solid var(--border)", fontSize: 12, fontWeight: 500, color: "var(--text-3)" }}>
                <span>Name</span><span>Type</span><span>Host / path</span><span>Status</span><span>Latency</span><span>Last checked</span><span />
              </div>
              {rows.map((c) => {
                const tile = dbTile(c.type, theme === "dark");
                const badge = statusBadge(testing.has(c.id) ? "testing" : c.status);
                const typeLabel = types[c.type]?.label || c.type;
                return (
                  <div key={c.id} className="hover-surface2" style={{ display: "grid", gridTemplateColumns: "minmax(210px,1.3fr) 130px minmax(220px,1.6fr) 124px 84px 100px 128px", gap: 12, alignItems: "center", padding: "0 16px", minHeight: 52, borderBottom: "1px solid var(--border)" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                      <span style={{ width: 28, height: 28, flexShrink: 0, borderRadius: 6, background: tile.tbg, color: tile.tfg, fontFamily: "'Geist Mono',monospace", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center" }}>{types[c.type]?.abbr || c.type.slice(0, 2).toUpperCase()}</span>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontWeight: 500, display: "flex", gap: 6, alignItems: "center" }}>
                          {c.name}
                          {c.env && <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 10, padding: "0 5px", borderRadius: 4, border: "1px solid var(--border-2)", color: "var(--text-3)" }}>{c.env}</span>}
                        </div>
                        <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)" }}>db: {c.database || "—"}</div>
                      </div>
                    </div>
                    <span style={{ color: "var(--text-2)" }}>{typeLabel}</span>
                    <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.host}</span>
                    <div style={{ display: "flex", flexDirection: "column", gap: 2, alignItems: "flex-start" }}>
                      <StatusBadge b={badge} />
                      {c.note && <span style={{ fontSize: 11, color: "var(--text-3)" }}>{c.note}</span>}
                    </div>
                    <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)" }}>{c.latency_ms != null ? (c.latency_ms < 1 ? "<1 ms" : `${Math.round(c.latency_ms)} ms`) : "—"}</span>
                    <span style={{ fontSize: 12, color: "var(--text-3)" }}>{relativeTime(c.last_checked_at)}</span>
                    <div style={{ display: "flex", gap: 4, justifyContent: "flex-end", position: "relative" }}>
                      <Button height={28} padX={10} icon="bolt" disabled={testing.has(c.id)} onClick={() => test(c.id)} style={{ fontSize: 12 }}>
                        {testing.has(c.id) ? "Testing…" : "Test"}
                      </Button>
                      <IconButton icon="upload" title="Load data into this connection" onClick={() => nav("/upload", { state: { targetConnId: c.id } })} />
                      <IconButton icon="more_horiz" title="More" onClick={() => setMenuFor(menuFor === c.id ? null : c.id)} />
                      {menuFor === c.id && (
                        <div style={{ position: "absolute", right: 0, top: 32, width: 140, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, boxShadow: "var(--shadow-2)", padding: 4, zIndex: 5 }}>
                          <button onClick={() => { setMenuFor(null); setEditing(c); }} className="hover-surface2" style={{ height: 30, width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "0 8px", border: "none", background: "transparent", borderRadius: 5, cursor: "pointer", textAlign: "left" }}>
                            <Icon name="edit" style={{ fontSize: 16 }} />Edit
                          </button>
                          <button onClick={() => { setMenuFor(null); remove(c.id, c.name); }} className="hover-surface2" style={{ height: 30, width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "0 8px", border: "none", background: "transparent", borderRadius: 5, cursor: "pointer", textAlign: "left", color: "var(--err)" }}>
                            <Icon name="delete" style={{ fontSize: 16 }} />Delete
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
              <div style={{ padding: "10px 16px", fontSize: 12, color: "var(--text-3)", display: "flex", gap: 6, alignItems: "center" }}>
                <Icon name="lock" style={{ fontSize: 15 }} />Credentials are encrypted at rest with the local master key. Background checks run periodically.
              </div>
            </div>
          </div>
        </>
      )}

      {addOpen && <AddConnectionModal initialTypes={Object.values(types)} onClose={() => setAddOpen(false)} onSaved={() => { setAddOpen(false); load(); }} />}
      {editing && <AddConnectionModal connection={editing} initialTypes={Object.values(types)} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </div>
  );
}
