import { useEffect, useRef, useState } from "react";
import { connectionsApi, databasesOnly, filesApi, type FileEntry } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { fmtBytes, dbTile, statusBadge } from "../../lib/format";
import { SUPPORTED_FILE_FORMATS } from "../../lib/constants";
import { Icon } from "../../ui/Icon";
import { Button } from "../../ui/Button";
import { StatusBadge } from "../../ui/Badge";
import { ProgressBar } from "../../ui/ProgressBar";
import { defaultColumnsFromNative, type SourceTab, type WizardState } from "./types";
import type { WizardCtx } from "./Wizard";

const TABS: { id: SourceTab; label: string; icon: string }[] = [
  { id: "upload", label: "Upload file", icon: "upload" },
  { id: "server", label: "Server folder", icon: "folder_open" },
  { id: "remote", label: "Remote · S3 / SFTP / URL", icon: "cloud" },
  { id: "database", label: "Database", icon: "database" },
];

interface Props { s: WizardState; set: (p: Partial<WizardState>) => void; ctx: WizardCtx; }

export function Step1Source({ s, set, ctx }: Props) {
  const toast = useToast();

  async function inspectPicked(path: string, name: string) {
    try {
      const insp = await filesApi.inspect(path, 20);
      set({ filePath: path, uploadedName: name, fileInspect: insp, columns: defaultColumnsFromNative(insp.columns) });
    } catch (e) {
      toast("err", "Could not read file", e instanceof Error ? e.message : undefined);
    }
  }

  return (
    <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
      <div style={{ display: "flex", gap: 4, padding: "0 16px", borderBottom: "1px solid var(--border)" }}>
        {TABS.map((t) => {
          const active = s.sourceTab === t.id;
          return (
            <button
              key={t.id}
              onClick={() => set({ sourceTab: t.id })}
              style={{ height: 42, display: "flex", alignItems: "center", gap: 7, padding: "0 10px", border: "none", borderBottom: `2px solid ${active ? "var(--accent)" : "transparent"}`, marginBottom: -1, background: "transparent", color: active ? "var(--text)" : "var(--text-2)", fontWeight: 500, cursor: "pointer" }}
            >
              <Icon name={t.icon} style={{ fontSize: 17 }} />{t.label}
            </button>
          );
        })}
      </div>
      <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 16 }}>
        {s.sourceTab === "upload" && <UploadTab s={s} set={set} onInspected={inspectPicked} />}
        {s.sourceTab === "server" && <ServerTab s={s} onPick={inspectPicked} />}
        {s.sourceTab === "remote" && <RemoteTab />}
        {s.sourceTab === "database" && <DatabaseTab s={s} set={set} ctx={ctx} />}
      </div>
    </div>
  );
}

// ---- Upload file --------------------------------------------------------------------

function UploadTab({ s, set, onInspected }: { s: WizardState; set: (p: Partial<WizardState>) => void; onInspected: (path: string, name: string) => Promise<void> }) {
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  async function upload(file: File) {
    set({ uploadState: "uploading", uploadPct: 0, uploadedName: file.name });
    try {
      const res = await filesApi.upload(file, (pct) => set({ uploadPct: pct }));
      set({ uploadState: "done", uploadPct: 1 });
      await onInspected(res.path, res.name);
    } catch (e) {
      set({ uploadState: "idle", uploadPct: 0 });
      toast("err", "Upload failed", e instanceof Error ? e.message : undefined);
    }
  }

  if (s.uploadState === "idle") {
    return (
      <>
        <input ref={inputRef} type="file" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        <button
          onClick={() => inputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files?.[0]; if (f) upload(f); }}
          style={{ minHeight: 230, border: `1.5px dashed ${dragOver ? "var(--accent)" : "var(--border-2)"}`, borderRadius: 10, background: dragOver ? "var(--accent-soft)" : "var(--surface-2)", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 10, cursor: "pointer", padding: 24, width: "100%" }}
        >
          <span style={{ width: 44, height: 44, borderRadius: 10, background: "var(--surface)", border: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--accent)" }}>
            <Icon name="upload_file" style={{ fontSize: 24 }} />
          </span>
          <span style={{ fontSize: 15, fontWeight: 500 }}>Drop a file here, or <span style={{ color: "var(--accent)" }}>browse</span></span>
          <span style={{ color: "var(--text-3)", fontSize: 12 }}>Up to 2 GB per file</span>
        </button>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{ fontSize: 12, color: "var(--text-3)", marginRight: 4 }}>Supported</span>
          {SUPPORTED_FILE_FORMATS.map((f) => <span key={f} style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, padding: "3px 7px", borderRadius: 5, border: "1px solid var(--border)", color: "var(--text-2)" }}>{f}</span>)}
        </div>
      </>
    );
  }

  const pct = Math.round(s.uploadPct * 100);
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ width: 36, height: 36, borderRadius: 8, background: "var(--surface-2)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-2)" }}><Icon name="description" /></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontFamily: "'Geist Mono',monospace", fontWeight: 500 }}>{s.uploadedName}</div>
          {s.fileInspect && <div style={{ fontSize: 12, color: "var(--text-3)" }}>{s.fileInspect.size_bytes != null ? fmtBytes(s.fileInspect.size_bytes) : ""} · {s.fileInspect.format}</div>}
        </div>
        {s.uploadState === "done" && <StatusBadge b={statusBadge("ok", "Uploaded")} />}
        <Button height={28} padX={10} onClick={() => set({ uploadState: "idle", uploadPct: 0, filePath: null, fileInspect: null, columns: [] })} style={{ fontSize: 12 }}>Replace</Button>
      </div>
      <ProgressBar pct={pct} color={s.uploadState === "done" ? "var(--ok)" : "var(--accent)"} height={6} />
      {s.uploadState === "done" && s.fileInspect && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 1, background: "var(--border)", border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
          <Stat label="Rows" value={s.fileInspect.row_estimate != null ? `${s.fileInspect.exact ? "" : "~"}${s.fileInspect.row_estimate.toLocaleString()}` : "unknown"} />
          <Stat label="Columns" value={String(s.fileInspect.columns.length)} />
          <Stat label="Detected" value={s.fileInspect.format.toUpperCase()} />
          <Stat label="Size" value={s.fileInspect.size_bytes != null ? fmtBytes(s.fileInspect.size_bytes) : "—"} />
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ background: "var(--surface)", padding: "10px 12px" }}>
      <div style={{ fontSize: 11, color: "var(--text-3)" }}>{label}</div>
      <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 14, marginTop: 2 }}>{value}</div>
    </div>
  );
}

// ---- Server folder --------------------------------------------------------------------

function ServerTab({ s, onPick }: { s: WizardState; onPick: (path: string, name: string) => void }) {
  const toast = useToast();
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [roots, setRoots] = useState<string[]>([]);

  const load = (p: string) => filesApi.list(p).then((es) => { setEntries(es); setPath(p); }).catch((e) => toast("err", "Could not list folder", e.message));

  useEffect(() => {
    load("");
    filesApi.roots().then(setRoots).catch(() => {});
  }, []);

  const norm = (p: string) => p.replace(/[\\/]+$/, "");
  const inside = (p: string) => roots.some((r) => { const n = norm(p), rn = norm(r); return n === rn || n.startsWith(rn + "/") || n.startsWith(rn + "\\"); });
  // "" is the default folder (the data dir); an allowed root itself has nowhere further up to go.
  const atTop = !path || roots.some((r) => norm(r) === norm(path));

  const parent = () => {
    const sep = path.includes("\\") ? "\\" : "/";
    const parts = path.split(sep).filter(Boolean);
    parts.pop();
    const up = (path.startsWith(sep) ? sep : "") + parts.join(sep);
    load(up && inside(up) ? up : "");
  };

  return (
    <>
      <div style={{ display: "flex", gap: 8 }}>
        <input value={path} onChange={(e) => setPath(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load(path)} style={{ flex: 1, height: 32, padding: "0 10px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", outline: "none", fontFamily: "'Geist Mono',monospace" }} />
        <Button icon="refresh" onClick={() => load(path)}>Rescan</Button>
      </div>
      <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
        <div style={{ display: "grid", gridTemplateColumns: "28px minmax(0,1fr) 90px 100px 130px", gap: 12, padding: "0 14px", height: 32, alignItems: "center", fontSize: 12, color: "var(--text-3)", fontWeight: 500, borderBottom: "1px solid var(--border)" }}>
          <span /><span>File</span><span>Format</span><span>Size</span><span>Modified</span>
        </div>
        {!atTop && (
          <button onClick={parent} className="hover-surface2" style={{ width: "100%", display: "grid", gridTemplateColumns: "28px minmax(0,1fr) 90px 100px 130px", gap: 12, padding: "0 14px", height: 34, alignItems: "center", border: "none", borderBottom: "1px solid var(--border)", background: "transparent", cursor: "pointer", textAlign: "left" }}>
            <Icon name="arrow_upward" style={{ fontSize: 15, color: "var(--text-3)" }} /><span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)" }}>..</span><span /><span /><span />
          </button>
        )}
        {entries?.map((f) => (
          <button
            key={f.path}
            onClick={() => (f.is_dir ? load(f.path) : f.format ? onPick(f.path, f.name) : toast("warn", `${f.name}: unsupported format`))}
            className="hover-surface2"
            style={{ width: "100%", display: "grid", gridTemplateColumns: "28px minmax(0,1fr) 90px 100px 130px", gap: 12, padding: "0 14px", height: 38, alignItems: "center", border: "none", borderBottom: "1px solid var(--border)", background: s.filePath === f.path ? "var(--accent-soft)" : "transparent", cursor: "pointer", textAlign: "left" }}
          >
            <Icon name={f.is_dir ? "folder" : "description"} style={{ fontSize: 16, color: "var(--text-3)" }} />
            <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12 }}>{f.name}</span>
            <span style={{ fontSize: 12, color: "var(--text-2)" }}>{f.is_dir ? "" : f.format || "—"}</span>
            <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)" }}>{f.is_dir || f.size_bytes == null ? "" : fmtBytes(f.size_bytes)}</span>
            <span style={{ fontSize: 12, color: "var(--text-3)" }}>{f.modified_at ? new Date(f.modified_at).toLocaleString() : ""}</span>
          </button>
        ))}
        {entries && entries.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>Empty folder.</div>}
      </div>
    </>
  );
}

// ---- Remote (not built) ----------------------------------------------------------------

function RemoteTab() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, alignItems: "flex-start" }}>
      <div style={{ display: "inline-flex", padding: 2, borderRadius: 7, background: "var(--surface-3)", opacity: 0.5 }}>
        {["Amazon S3", "SFTP", "HTTP URL"].map((l) => <span key={l} style={{ height: 28, padding: "0 12px", display: "flex", alignItems: "center", borderRadius: 5, fontWeight: 500, fontSize: 13 }}>{l}</span>)}
      </div>
      <div style={{ display: "flex", gap: 8, padding: "10px 12px", borderRadius: 6, background: "var(--warn-soft)", color: "var(--warn)", fontSize: 12, alignItems: "flex-start" }}>
        <Icon name="info" style={{ fontSize: 16 }} />
        <span style={{ color: "var(--text)" }}>Remote intake (S3 / SFTP / URL) isn&rsquo;t built yet. Use Upload or Server folder for now.</span>
      </div>
    </div>
  );
}

// ---- Database ---------------------------------------------------------------------------

function DatabaseTab({ s, set, ctx }: Props) {
  const toast = useToast();
  const [q, setQ] = useState("");

  useEffect(() => {
    if (s.sourceConnId && !s.tables) {
      connectionsApi.tables(s.sourceConnId).then((tables) => set({ tables })).catch((e) => toast("err", "Could not list tables", e.message));
    }
  }, [s.sourceConnId]);

  function pickConn(id: string) {
    set({ sourceConnId: id, tables: null, sourceTable: null, tableSchema: null, columns: [] });
  }

  async function pickTable(name: string) {
    set({ sourceTable: name, tableSchema: null });
    try {
      const schema = await connectionsApi.inspect(s.sourceConnId!, name);
      set({ tableSchema: schema, columns: defaultColumnsFromNative(schema.columns) });
    } catch (e) {
      toast("err", "Could not inspect table", e instanceof Error ? e.message : undefined);
    }
  }

  const ql = q.toLowerCase();
  const tables = (s.tables || []).filter((t) => !ql || t.name.toLowerCase().includes(ql));

  return (
    <>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Source connection</span>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(210px,1fr))", gap: 8 }}>
          {databasesOnly(ctx.conns).map((c) => {
            const tile = dbTile(c.type, ctx.dark);
            const active = s.sourceConnId === c.id;
            return (
              <button key={c.id} onClick={() => pickConn(c.id)} className="hover-surface2" style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", borderRadius: 8, border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`, boxShadow: active ? "0 0 0 3px var(--accent-soft)" : "none", background: "var(--surface)", cursor: "pointer", textAlign: "left" }}>
                <span style={{ width: 28, height: 28, borderRadius: 6, background: tile.tbg, color: tile.tfg, fontFamily: "'Geist Mono',monospace", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>{ctx.dbTypes[c.type]?.abbr || c.type.slice(0, 2).toUpperCase()}</span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: "block", fontWeight: 500 }}>{c.name}</span>
                  <span style={{ display: "block", fontSize: 11, color: "var(--text-3)" }}>{ctx.dbTypes[c.type]?.label || c.type}</span>
                </span>
                <Icon name={statusBadge(c.status).icon} style={{ fontSize: 16, color: statusBadge(c.status).fg }} />
              </button>
            );
          })}
        </div>
      </div>

      {s.sourceConnId && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <div style={{ display: "inline-flex", padding: 2, borderRadius: 7, background: "var(--surface-3)" }}>
              {(["table", "query"] as const).filter((v) => v !== "query" || ctx.conns.find((c) => c.id === s.sourceConnId)?.type !== "mongodb").map((v) => (
                <button key={v} onClick={() => set({ sourceView: v })} style={{ height: 28, padding: "0 14px", borderRadius: 5, border: "none", background: s.sourceView === v ? "var(--surface)" : "transparent", color: s.sourceView === v ? "var(--text)" : "var(--text-2)", boxShadow: s.sourceView === v ? "var(--shadow-1)" : "none", fontWeight: 500, cursor: "pointer" }}>
                  {v === "table" ? "Table" : "SQL query"}
                </button>
              ))}
            </div>
            <div style={{ flex: 1 }} />
            {s.sourceView === "table" && (
              <div style={{ width: 280, height: 30, display: "flex", alignItems: "center", gap: 6, padding: "0 10px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", color: "var(--text-3)" }}>
                <Icon name="search" style={{ fontSize: 16 }} />
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search tables" style={{ flex: 1, minWidth: 0, border: "none", background: "transparent", outline: "none", color: "var(--text)", fontFamily: "'Geist Mono',monospace", fontSize: 12 }} />
              </div>
            )}
          </div>

          {s.sourceView === "table" && (
            <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}>
              <div style={{ minWidth: 620 }}>
                <div style={{ display: "grid", gridTemplateColumns: "28px minmax(0,1fr) 130px 90px 190px", gap: 12, padding: "0 14px", height: 32, alignItems: "center", fontSize: 12, color: "var(--text-3)", fontWeight: 500, borderBottom: "1px solid var(--border)" }}>
                  <span /><span>{s.tables ? `${s.tables.length} tables` : "Tables"}</span><span>Rows</span><span>Size</span><span>Info</span>
                </div>
                {tables.map((t) => {
                  const on = t.name === s.sourceTable;
                  return (
                    <button key={t.name} onClick={() => pickTable(t.name)} className="hover-surface2" style={{ width: "100%", display: "grid", gridTemplateColumns: "28px minmax(0,1fr) 130px 90px 190px", gap: 12, padding: "0 14px", height: 40, alignItems: "center", border: "none", borderBottom: "1px solid var(--border)", background: on ? "var(--accent-soft)" : "transparent", cursor: "pointer", textAlign: "left" }}>
                      <span style={{ width: 14, height: 14, borderRadius: "50%", border: `1.5px solid ${on ? "var(--accent)" : "var(--border-2)"}`, display: "flex", alignItems: "center", justifyContent: "center" }}>{on && <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--accent)" }} />}</span>
                      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, fontWeight: 500 }}>{t.name}</span>
                      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)", display: "flex", gap: 6, alignItems: "center" }}>{t.est_rows != null ? `~${t.est_rows.toLocaleString()}` : "?"}<span style={{ fontSize: 10, color: "var(--text-3)" }}>{t.exact ? "exact" : "est."}</span></span>
                      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)" }}>{t.size_bytes != null ? fmtBytes(t.size_bytes) : "—"}</span>
                      <span style={{ fontSize: 12, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.info}</span>
                    </button>
                  );
                })}
                {s.tables && tables.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>No tables match.</div>}
              </div>
            </div>
          )}

          {s.sourceView === "query" && (
            <>
              <textarea
                value={s.sourceQuery}
                onChange={(e) => set({ sourceQuery: e.target.value })}
                spellCheck={false}
                rows={6}
                placeholder="SELECT * FROM public.orders WHERE status = 'paid'"
                style={{ width: "100%", padding: 10, border: "1px solid var(--border)", borderRadius: 6, background: "var(--code-bg)", color: "var(--text)", outline: "none", fontFamily: "'Geist Mono',monospace", fontSize: 12, lineHeight: "20px", resize: "vertical" }}
              />
              <span style={{ fontSize: 12, color: "var(--text-3)" }}>This query is validated against the target once you continue to Preview &amp; plan.</span>
            </>
          )}
        </>
      )}
    </>
  );
}
