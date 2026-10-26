import { useRef, useState } from "react";
import { sessionEnded, transferApi, type ImportItem, type ImportPlan, type ImportResult, type ImportSection } from "../../lib/api";
import { useCan } from "../../lib/auth";
import { useToast } from "../../lib/toast";
import { Button } from "../../ui/Button";
import { Icon } from "../../ui/Icon";
import { Input } from "../../ui/Input";
import { ToggleRow } from "../../ui/Toggle";
import { Panel } from "./Alerts";

const SECTION: Record<ImportSection, string> = {
  settings: "Settings", connections: "Connections", service_checks: "Service checks", pipelines: "Pipelines",
  alert_channels: "Alert channels", alert_rules: "Alert rules",
};
const ACTION: Record<ImportItem["action"], { color: string; bg: string }> = {
  create: { color: "var(--ok)", bg: "var(--ok-soft)" },
  update: { color: "var(--accent)", bg: "var(--accent-soft)" },
  skip: { color: "var(--text-3)", bg: "var(--surface-2)" },
  conflict: { color: "var(--err)", bg: "var(--err-soft)" },
};
const MIN_PASSPHRASE = 12;

function errText(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong";
}

function ExportPanel() {
  const can = useCan();
  const toast = useToast();
  const [secrets, setSecrets] = useState(false);
  const [pass, setPass] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const problem = !secrets ? null : pass.length < MIN_PASSPHRASE ? `At least ${MIN_PASSPHRASE} characters` : pass !== again ? "The two passphrases differ" : null;

  const download = async () => {
    setBusy(true);
    try {
      const { blob, filename } = await transferApi.exportFile(secrets, secrets ? pass : null);
      const url = URL.createObjectURL(blob);
      const a = Object.assign(document.createElement("a"), { href: url, download: filename });
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("ok", `Exported ${filename}`, secrets ? "Secrets are encrypted with your passphrase: keep it apart from the file." : "No secrets in the file.");
      setPass(""); setAgain("");
    } catch (e) {
      if (!sessionEnded(e)) toast("err", "Export failed", errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel icon="download" title="Export" sub="this Conduit's configuration as one JSON file">
      <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ fontSize: 13, color: "var(--text-2)" }}>
          Connections, service checks, pipelines (steps, schedule, on/off), alert channels and rules, and the settings saved on the Settings tab.
          References are by name. Never in the file: users, jobs, comparisons, watermarks, loaded-file records, alert state, .env values.
        </div>
        {can.manageUsers ? (
          <ToggleRow on={secrets} onClick={() => setSecrets(!secrets)} title="Include secrets"
            sub="Passwords, tokens and webhook URLs, encrypted with a passphrase you choose (never this instance's key). Without them, imported connections need their credentials re-entered." />
        ) : (
          <div style={{ fontSize: 12, color: "var(--text-3)", display: "flex", gap: 6, alignItems: "center" }}>
            <Icon name="lock" style={{ fontSize: 15 }} />The file has no secrets. Only a superadmin can export them.
          </div>
        )}
        {secrets && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, maxWidth: 620 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: "var(--text-2)" }}>
              Passphrase
              <Input type="password" autoComplete="new-password" value={pass} onChange={(e) => setPass(e.target.value)} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: "var(--text-2)" }}>
              Again
              <Input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
            </label>
            <div style={{ gridColumn: "1 / -1", fontSize: 11, color: problem && (pass || again) ? "var(--err)" : "var(--text-3)" }}>
              {problem && (pass || again) ? problem : `At least ${MIN_PASSPHRASE} characters. Whoever has the file and the passphrase has the secrets: send them separately.`}
            </div>
          </div>
        )}
        <div>
          <Button variant="primary" icon="download" disabled={busy || !!problem} onClick={download}>{busy ? "Exporting…" : "Download export"}</Button>
        </div>
      </div>
    </Panel>
  );
}

function ImportPanel() {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [doc, setDoc] = useState<Record<string, unknown> | null>(null);
  const [fileName, setFileName] = useState("");
  const [pass, setPass] = useState("");
  const [plan, setPlan] = useState<ImportPlan | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [results, setResults] = useState<Map<string, ImportResult> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const encrypted = doc?.secrets === "encrypted";

  const choose = async (f: File) => {
    setPlan(null); setResults(null); setError(null); setPass(""); setFileName(f.name);
    try {
      const parsed = JSON.parse(await f.text());
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
      setDoc(parsed);
    } catch {
      setDoc(null);
      setError(`${f.name} is not a JSON file`);
    }
  };

  const dryRun = async () => {
    if (!doc) return;
    setBusy(true); setError(null); setResults(null);
    try {
      const p = await transferApi.plan(doc, encrypted && pass ? pass : null);
      setPlan(p);
      setPicked(new Set(p.items.filter((i) => i.action === "create" || i.action === "update").map((i) => i.key)));
    } catch (e) {
      if (!sessionEnded(e)) { setPlan(null); setError(errText(e)); }
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    if (!doc || !plan) return;
    setBusy(true);
    try {
      const res = await transferApi.apply(doc, encrypted && pass ? pass : null, [...picked]);
      setResults(new Map(res.map((r) => [r.key, r])));
      const bad = res.filter((r) => !r.ok).length;
      toast(bad ? "err" : "ok", `Imported ${res.length - bad} of ${res.length}`, bad ? "See the failed rows below." : "Imported pipelines are disabled until their preview is refreshed.");
    } catch (e) {
      if (!sessionEnded(e)) toast("err", "Import failed", errText(e));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (key: string) => setPicked((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  const sections = plan ? (Object.keys(SECTION) as ImportSection[]).filter((s) => plan.items.some((i) => i.section === s)) : [];

  return (
    <Panel icon="upload" title="Import" sub="a dry run first, then only what you pick">
      <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
          <input ref={fileRef} type="file" accept=".json,application/json" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) choose(f); e.target.value = ""; }} />
          <Button icon="upload_file" onClick={() => fileRef.current?.click()}>{fileName ? "Another file" : "Choose export file"}</Button>
          {fileName && <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 12, color: "var(--text-2)", paddingBottom: 8 }}>{fileName}</span>}
          {encrypted && (
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: "var(--text-2)", width: 260 }}>
              Passphrase (empty: import without secrets)
              <Input type="password" autoComplete="off" value={pass} onChange={(e) => setPass(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") dryRun(); }} />
            </label>
          )}
          {doc && <Button variant="primary" icon="fact_check" disabled={busy} onClick={dryRun}>{busy && !plan ? "Checking…" : "Dry run"}</Button>}
        </div>
        {error && <div style={{ color: "var(--err)", fontSize: 13, display: "flex", gap: 6, alignItems: "center" }}><Icon name="error" style={{ fontSize: 16 }} />{error}</div>}
        {plan && (
          <>
            <div style={{ fontSize: 13, color: "var(--text-2)", display: "flex", flexDirection: "column", gap: 4 }}>
              <div>
                From <b style={{ fontWeight: 600, color: "var(--text)" }}>{plan.instance}</b>
                {plan.exported_at && <>, exported {new Date(plan.exported_at).toLocaleString()}</>}
                {" · "}{plan.secrets === "encrypted" ? (plan.secrets_used ? "secrets included" : "secrets left out") : "no secrets"}
              </div>
              {plan.notes.map((n) => <div key={n} style={{ display: "flex", gap: 6, alignItems: "flex-start", fontSize: 12 }}><Icon name="info" style={{ fontSize: 15, color: "var(--text-3)" }} />{n}</div>)}
            </div>
            <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
              {sections.map((s) => (
                <div key={s}>
                  <div style={{ padding: "8px 12px", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em", color: "var(--text-3)", background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}>{SECTION[s]}</div>
                  {plan.items.filter((i) => i.section === s).map((i) => {
                    const r = results?.get(i.key);
                    const can = i.action === "create" || i.action === "update";
                    return (
                      <label key={i.key} style={{ display: "grid", gridTemplateColumns: "24px 80px minmax(160px, 0.8fr) minmax(0, 2fr)", gap: 10, alignItems: "start", padding: "8px 12px", borderBottom: "1px solid var(--border)", cursor: can ? "pointer" : "default", opacity: can || r ? 1 : 0.75 }}>
                        <input type="checkbox" disabled={!can || busy} checked={picked.has(i.key)} onChange={() => toggle(i.key)} style={{ marginTop: 3 }} />
                        <span style={{ justifySelf: "start", fontSize: 11, fontWeight: 600, padding: "1px 7px", borderRadius: 4, color: ACTION[i.action].color, background: ACTION[i.action].bg }}>{i.action}</span>
                        <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={i.name}>{i.name}</span>
                        <div style={{ fontSize: 12, color: "var(--text-2)", display: "flex", flexDirection: "column", gap: 2 }}>
                          {i.notes.map((n) => <span key={n}>{n}</span>)}
                          {r && <span style={{ color: r.ok ? "var(--ok)" : "var(--err)", display: "flex", gap: 4, alignItems: "center" }}>
                            <Icon name={r.ok ? "check_circle" : "error"} style={{ fontSize: 14 }} />{r.ok ? (r.action === "create" ? "Created" : "Updated") : r.error}
                          </span>}
                        </div>
                      </label>
                    );
                  })}
                </div>
              ))}
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Button variant="primary" icon="file_download_done" disabled={busy || picked.size === 0 || !!results} onClick={apply}>
                {busy ? "Importing…" : `Apply ${picked.size} picked`}
              </Button>
              {results && <Button icon="refresh" disabled={busy} onClick={dryRun}>Dry run again</Button>}
              <span style={{ fontSize: 12, color: "var(--text-3)" }}>Conflicts can't be picked. Pipelines arrive disabled: refresh their preview, then enable them.</span>
            </div>
          </>
        )}
      </div>
    </Panel>
  );
}

/** Control & Setup > Export / import. */
export function TransferTab() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <ExportPanel />
      <ImportPanel />
    </div>
  );
}
