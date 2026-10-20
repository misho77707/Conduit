import { useEffect, useMemo, useState } from "react";
import { remoteApi, type ConnectionOut, type RemoteEntryOut, type RemoteIn, type RemoteMatch, type RemoteMatchOut } from "../../lib/api";
import { fmtBytes } from "../../lib/format";
import { Icon } from "../../ui/Icon";
import { Button } from "../../ui/Button";
import { Input, Select } from "../../ui/Input";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ToggleRow } from "../../ui/Toggle";
import { AddConnectionModal } from "../AddConnectionModal";

export type RemoteKind = "s3" | "sftp" | "url";

export const emptyRemote = (connection_id: string | null = null): RemoteIn =>
  ({ connection_id, path: "", match: "exact", skip_loaded: true });

/** Which tab a saved remote source belongs to. */
export function kindOf(r: RemoteIn | null | undefined, conns: ConnectionOut[]): RemoteKind {
  if (!r) return "s3";
  if (!r.connection_id) return r.path ? "url" : "s3"; // no connection: an ad-hoc URL (or nothing chosen yet)
  const t = conns.find((c) => c.id === r.connection_id)?.type;
  return t === "sftp" ? "sftp" : t === "http" ? "url" : "s3";
}

/** A short, safe label for a remote source: the connection's host + path, a URL without its query string. */
export function remoteLabel(r: RemoteIn, conns: ConnectionOut[]): string {
  const host = conns.find((c) => c.id === r.connection_id)?.host;
  const path = r.path.split("?")[0].split("#")[0];
  return host ? `${host.replace(/\/$/, "")}/${path.replace(/^\//, "")}` : path;
}

const MATCH_HELP: Record<RemoteMatch, string> = {
  exact: "One specific file.",
  newest: "The most recently modified file matching the pattern, each run. Made for daily drops.",
  all: "Every matching file into the same target, in one job (one split per file).",
};

function extOf(name: string): string {
  const low = name.toLowerCase();
  for (const e of [".csv.gz", ".tsv.gz", ".json.gz", ".ndjson.gz", ".jsonl.gz"]) if (low.endsWith(e)) return e;
  const i = low.lastIndexOf(".");
  return i > 0 ? low.slice(i) : "";
}

/**
 * Choose a file (or a pattern of files) on an S3 / SFTP connection, or a URL. Emits a RemoteIn; inspecting it
 * is up to the caller. `needsFormat` shows a format picker (a URL whose name says nothing about its format).
 * `asInput`: an input of a combined source, which reads every matched file each run (no skip-loaded).
 */
export function RemotePicker({ value, onChange, conns, onConnsChanged, needsFormat, asInput }: {
  value: RemoteIn | null; onChange: (v: RemoteIn) => void; conns: ConnectionOut[]; onConnsChanged?: () => void; needsFormat?: boolean;
  asInput?: boolean;
}) {
  const [kind, setKind] = useState<RemoteKind>(() => kindOf(value, conns));
  const [folder, setFolder] = useState("");
  const [entries, setEntries] = useState<RemoteEntryOut[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listing, setListing] = useState(false);
  const [summary, setSummary] = useState<RemoteMatchOut | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const v = value || emptyRemote();
  const ofKind = conns.filter((c) => c.type === (kind === "url" ? "http" : kind));
  const conn = conns.find((c) => c.id === v.connection_id);

  // the folder a saved exact path / pattern lives in, so reopening shows it
  useEffect(() => {
    const p = v.path.includes("*") || v.path.includes("?") ? v.path.split("/").filter((s) => !/[*?[]/.test(s)).join("/") : v.path.split("/").slice(0, -1).join("/");
    setFolder(kind === "url" ? "" : p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [v.connection_id]);

  function pickKind(k: RemoteKind) {
    setKind(k);
    setEntries(null);
    setSummary(null);
    const first = conns.find((c) => c.type === (k === "url" ? "http" : k));
    onChange(emptyRemote(k === "url" ? null : first?.id || null));
  }

  useEffect(() => {
    if (kind === "url" || !v.connection_id) { setEntries(null); return; }
    let stop = false;
    setListing(true);
    setListError(null);
    remoteApi.list(v.connection_id, folder)
      .then((es) => { if (!stop) setEntries(es); })
      .catch((e) => { if (!stop) { setEntries(null); setListError(e instanceof Error ? e.message : "Could not list"); } })
      .finally(() => { if (!stop) setListing(false); });
    return () => { stop = true; };
  }, [kind, v.connection_id, folder]);

  // live match summary for a pattern
  useEffect(() => {
    setSummaryError(null);
    if (v.match === "exact" || !v.path.trim() || (kind !== "url" && !v.connection_id)) { setSummary(null); return; }
    const t = setTimeout(() => {
      remoteApi.match(v).then(setSummary).catch((e) => { setSummary(null); setSummaryError(e instanceof Error ? e.message : "Could not match"); });
    }, 500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [v.connection_id, v.path, v.match]);

  function setMatch(m: RemoteMatch) {
    if (m === v.match) return;
    if (m === "exact") return onChange({ ...v, match: m, path: "" });
    const common = (entries || []).filter((e) => !e.is_dir && e.format).map((e) => extOf(e.name));
    const ext = extOf(v.path) || common.sort((a, b) => common.filter((x) => x === b).length - common.filter((x) => x === a).length)[0] || ".csv";
    onChange({ ...v, match: m, path: `${folder ? folder + "/" : ""}*${ext}` });
  }

  const crumbs = useMemo(() => folder.split("/").filter(Boolean), [folder]);
  const label = (t: string) => <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>{t}</span>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <SegmentedControl options={[{ id: "s3", label: "S3" }, { id: "sftp", label: "SFTP" }, { id: "url", label: "URL" }]} value={kind} onChange={pickKind} />

      {kind !== "url" && (
        ofKind.length === 0 ? (
          <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "12px 14px", borderRadius: 8, border: "1px dashed var(--border-2)" }}>
            <Icon name="cloud_off" style={{ color: "var(--text-3)" }} />
            <span style={{ flex: 1, color: "var(--text-2)" }}>No {kind === "s3" ? "S3" : "SFTP"} connection yet. Credentials live in a saved connection, never in a load.</span>
            <Button icon="add" onClick={() => setAdding(true)}>Add connection</Button>
          </div>
        ) : (
          <>
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {label("Connection")}
              <Select value={v.connection_id || ""} onChange={(e) => { setFolder(""); onChange(emptyRemote(e.target.value || null)); }}>
                {ofKind.map((c) => <option key={c.id} value={c.id}>{c.name} · {c.host}</option>)}
              </Select>
            </label>

            <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 4, padding: "0 10px", height: 36, borderBottom: "1px solid var(--border)", background: "var(--surface-2)", fontFamily: "'Geist Mono',monospace", fontSize: 12, overflowX: "auto" }}>
                <button onClick={() => setFolder("")} style={crumb}>{conn?.host || "/"}</button>
                {crumbs.map((c, i) => (
                  <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <span style={{ color: "var(--text-3)" }}>/</span>
                    <button onClick={() => setFolder(crumbs.slice(0, i + 1).join("/"))} style={crumb}>{c}</button>
                  </span>
                ))}
                <div style={{ flex: 1 }} />
                {listing && <Icon name="progress_activity" spin style={{ fontSize: 15, color: "var(--text-3)" }} />}
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "28px minmax(0,1fr) 90px 90px 140px", gap: 12, padding: "0 14px", height: 30, alignItems: "center", fontSize: 12, color: "var(--text-3)", fontWeight: 500, borderBottom: "1px solid var(--border)" }}>
                <span /><span>Name</span><span>Format</span><span>Size</span><span>Modified</span>
              </div>
              <div style={{ maxHeight: 300, overflowY: "auto" }}>
                {folder && (
                  <button onClick={() => setFolder(crumbs.slice(0, -1).join("/"))} className="hover-surface2" style={row}>
                    <Icon name="arrow_upward" style={{ fontSize: 15, color: "var(--text-3)" }} /><span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)" }}>..</span><span /><span /><span />
                  </button>
                )}
                {entries?.map((e) => {
                  const on = !e.is_dir && v.match === "exact" && v.path === e.path;
                  const usable = e.is_dir || !!e.format || e.compression === "zip";
                  return (
                    <button key={e.path} onClick={() => (e.is_dir ? setFolder(e.path) : usable && onChange({ ...v, path: e.path, match: "exact" }))}
                      className="hover-surface2" title={usable ? undefined : "Not a supported format"}
                      style={{ ...row, background: on ? "var(--accent-soft)" : "transparent", opacity: usable ? 1 : 0.5, cursor: usable ? "pointer" : "default" }}>
                      <Icon name={e.is_dir ? "folder" : "description"} style={{ fontSize: 16, color: on ? "var(--accent)" : "var(--text-3)" }} />
                      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.name}</span>
                      <span style={{ fontSize: 12, color: "var(--text-2)" }}>{e.is_dir ? "" : e.format ? `${e.format}${e.compression ? ` · ${e.compression}` : ""}` : e.compression || "—"}</span>
                      <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)" }}>{e.is_dir || e.size == null ? "" : fmtBytes(e.size)}</span>
                      <span style={{ fontSize: 12, color: "var(--text-3)" }}>{e.modified ? new Date(e.modified).toLocaleString() : ""}</span>
                    </button>
                  );
                })}
                {entries && entries.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>Empty folder.</div>}
                {listError && <div style={{ padding: 14, fontSize: 12, color: "var(--err)" }}>{listError}</div>}
              </div>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {label("Which files")}
              <SegmentedControl small value={v.match} onChange={setMatch}
                options={[{ id: "exact", label: "Exact file" }, { id: "newest", label: "Newest match" }, { id: "all", label: "All matches" }]} />
              <span style={{ fontSize: 12, color: "var(--text-3)" }}>{asInput && v.match === "all" ? "Every matching file, read together as this input on each run." : MATCH_HELP[v.match]}</span>
              {v.match === "exact" ? (
                <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: v.path ? "var(--text)" : "var(--text-3)" }}>{v.path || "Click a file above."}</div>
              ) : (
                <>
                  <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {label("Pattern · * and ? within a folder, ** across folders")}
                    <Input value={v.path} onChange={(e) => onChange({ ...v, path: e.target.value })} placeholder="exports/orders_*.csv" />
                  </label>
                  <MatchLine summary={summary} error={summaryError} />
                  {!asInput && <ToggleRow on={v.skip_loaded} onClick={() => onChange({ ...v, skip_loaded: !v.skip_loaded })} title="Skip files already loaded into this target"
                    sub="Each file is remembered once its rows are in the target. A file overwritten in place counts as changed and loads again." />}
                </>
              )}
            </div>
          </>
        )
      )}

      {kind === "url" && (
        <>
          <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {label("HTTP connection · optional")}
            <Select value={v.connection_id || ""} onChange={(e) => onChange({ ...v, connection_id: e.target.value || null, path: "" })}>
              <option value="">None: a public URL</option>
              {ofKind.map((c) => <option key={c.id} value={c.id}>{c.name} · {c.host}</option>)}
            </Select>
            <span style={{ fontSize: 11, color: "var(--text-3)" }}>Only needed when the URL wants credentials (its headers are stored encrypted in the connection).</span>
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {label(conn ? `Path under ${conn.host}` : "URL")}
            <Input value={v.path} onChange={(e) => onChange({ ...v, path: e.target.value, match: "exact" })} placeholder={conn ? "exports/orders.csv" : "https://example.com/data/orders.csv"} />
          </label>
        </>
      )}

      {needsFormat && (
        <label style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: 260 }}>
          {label("File format")}
          <Select value={v.format || ""} onChange={(e) => onChange({ ...v, format: (e.target.value || null) as RemoteIn["format"] })}>
            <option value="">Choose…</option>
            <option value="csv">CSV / TSV</option>
            <option value="json">JSON / NDJSON</option>
            <option value="parquet">Parquet</option>
            <option value="excel">Excel (.xlsx)</option>
          </Select>
          <span style={{ fontSize: 11, color: "var(--text-3)" }}>The name of this file does not say what it is.</span>
        </label>
      )}

      {adding && <AddConnectionModal onClose={() => setAdding(false)} onSaved={() => { setAdding(false); onConnsChanged?.(); }} />}
    </div>
  );
}

export function MatchLine({ summary, error }: { summary: RemoteMatchOut | null; error?: string | null }) {
  if (error) return <div style={{ fontSize: 12, color: "var(--err)" }}>{error}</div>;
  if (!summary) return null;
  const s = summary;
  const parts = [`matches ${s.total.toLocaleString()} file${s.total === 1 ? "" : "s"}`, fmtBytes(s.total_bytes)];
  if (s.known_status) {
    if (s.new_count) parts.push(`${s.new_count} new`);
    if (s.changed_count) parts.push(`${s.changed_count} changed`);
    if (s.loaded_count) parts.push(`${s.loaded_count} already loaded`);
  }
  const newest = s.match === "newest" && s.files[0] ? s.files[0] : null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "8px 12px", borderRadius: 6, background: s.total ? "var(--surface-2)" : "var(--warn-soft)", fontSize: 12 }}>
      <span style={{ fontWeight: 500 }}>{parts.join(" · ")}</span>
      {newest && <span style={{ fontFamily: "'Geist Mono',monospace", color: "var(--text-2)" }}>newest: {newest.path}{newest.modified ? ` · ${new Date(newest.modified).toLocaleString()}` : ""}</span>}
      {s.match === "all" && s.files.length > 0 && (
        <span style={{ fontFamily: "'Geist Mono',monospace", color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {s.files.slice(0, 4).map((f) => f.path).join(", ")}{s.total > 4 ? ` … +${s.total - 4}` : ""}
        </span>
      )}
      {s.notes.map((n, i) => <span key={i} style={{ color: "var(--text-3)" }}>{n}</span>)}
    </div>
  );
}

const crumb = { border: "none", background: "transparent", color: "var(--text-2)", cursor: "pointer", padding: "2px 4px", borderRadius: 4, fontFamily: "'Geist Mono',monospace", fontSize: 12, whiteSpace: "nowrap" as const };
const row = { width: "100%", display: "grid", gridTemplateColumns: "28px minmax(0,1fr) 90px 90px 140px", gap: 12, padding: "0 14px", height: 36, alignItems: "center", border: "none", borderBottom: "1px solid var(--border)", background: "transparent", cursor: "pointer", textAlign: "left" as const };
