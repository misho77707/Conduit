import { useEffect, useRef, useState, type CSSProperties } from "react";
import { filesApi, remoteApi } from "../../lib/api";
import { MatchLine } from "../remote/RemotePicker";
import { AUTO_TYPE, LOGICAL_TYPES, TIMEZONES } from "../../lib/constants";
import { fileFormatLabel, fmtN } from "../../lib/format";
import { Icon } from "../../ui/Icon";
import { Card, CardHeader } from "../../ui/Card";
import { Select } from "../../ui/Input";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ToggleRow } from "../../ui/Toggle";
import type { SplitCandidateOut } from "../../lib/api";
import { defaultColumnsFromNative, isFileLike, type ColumnMapUI, type Strategy, type WizardState } from "./types";
import type { WizardCtx } from "./Wizard";

interface Props { s: WizardState; set: (p: Partial<WizardState>) => void; ctx: WizardCtx; }

export function Step2Preview({ s, set, ctx }: Props) {
  return isFileLike(s) ? <FilePreview s={s} set={set} /> : <DatabasePlan s={s} set={set} ctx={ctx} />;
}

// ---- File: detected format + column grid + sample -------------------------------------

function FilePreview({ s, set }: { s: WizardState; set: (p: Partial<WizardState>) => void }) {
  const insp = s.fileInspect;
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const first = useRef(true);

  // header / rows-to-skip change what the columns are: read the file again with them (local or remote)
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    const t = setTimeout(() => {
      setReading(true);
      setReadError(null);
      const opts = { header: s.fileHeader, skip_rows: s.fileSkipRows };
      const req = s.remote ? remoteApi.inspect({ ...s.remote.spec, ...opts }, 20) : s.filePath ? filesApi.inspect(s.filePath, 20, opts) : null;
      req?.then((res) => set({ fileInspect: res, columns: defaultColumnsFromNative(res.columns) }))
        .catch((e) => setReadError(e instanceof Error ? e.message : "Could not read the file"))
        .finally(() => setReading(false));
    }, 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.fileHeader, s.fileSkipRows]);

  if (!insp) return null;
  const updCol = (i: number, patch: Partial<ColumnMapUI>) => set({ columns: s.columns.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
  const included = s.columns.filter((c) => c.include).length;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(220px,260px) minmax(0,1fr)", gap: 16, alignItems: "start" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Card>
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ fontWeight: 600 }}>Detected format</div>
            {s.remote && <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-2)", overflowWrap: "anywhere" }}>{s.remote.display}</div>}
            <Row label="Format" value={fileFormatLabel(insp.format, insp.compression)} />
            {reading && <div style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, color: "var(--text-3)" }}><Icon name="progress_activity" spin style={{ fontSize: 14 }} />Reading again…</div>}
            {readError && <div style={{ fontSize: 12, color: "var(--err)" }}>{readError}</div>}
            {insp.format === "csv" && (
              <label style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <input type="number" min={0} value={s.fileSkipRows} onChange={(e) => set({ fileSkipRows: Math.max(0, Number(e.target.value)) })} style={{ width: 56, height: 28, padding: "0 8px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", fontFamily: "'Geist Mono',monospace", fontSize: 12 }} />
                <span style={{ fontSize: 12, color: "var(--text-2)" }}>rows to skip before the header</span>
              </label>
            )}
            {(insp.format === "csv" || insp.format === "excel") && (
              <ToggleRow on={s.fileHeader} onClick={() => set({ fileHeader: !s.fileHeader })} title="First row is header" />
            )}
          </div>
        </Card>
        {s.remote && s.remote.spec.match !== "exact" && (
          <Card>
            <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ fontWeight: 600 }}>{s.remote.spec.match === "all" ? "All matching files" : "Newest matching file"}</div>
              <MatchLine summary={s.remote.match} />
              <div style={{ fontSize: 12, color: "var(--text-3)" }}>{s.remote.spec.match === "all" ? "The preview shows the first file; every file needs the same column names." : "Each run picks the newest file again."}</div>
            </div>
          </Card>
        )}
        {insp.notes.length > 0 && (
          <Card>
            <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ fontWeight: 600 }}>Notes</div>
              {insp.notes.map((n, i) => (
                <div key={i} style={{ display: "flex", gap: 8 }}>
                  <Icon name="info" style={{ fontSize: 16, color: "var(--accent)" }} />
                  <div style={{ fontSize: 12, color: "var(--text-2)" }}>{n}</div>
                </div>
              ))}
            </div>
          </Card>
        )}
      </div>

      <Card style={{ display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
          <div style={{ fontWeight: 600 }}>Preview</div>
          <span style={{ fontSize: 12, color: "var(--text-3)" }}>
            First {insp.preview.length} of {insp.row_estimate != null ? `${insp.exact ? "" : "~"}${insp.row_estimate.toLocaleString()}` : "unknown"} rows · {included} of {s.columns.length} columns included
          </span>
        </div>
        <div style={{ overflow: "auto", maxHeight: 560 }}>
          <table style={{ borderCollapse: "separate", borderSpacing: 0, fontSize: 12, minWidth: "100%" }}>
            <thead>
              <tr>
                <th style={thNum}>#</th>
                {s.columns.map((c, i) => (
                  <th key={c.source} style={{ position: "sticky", top: 0, zIndex: 2, background: "var(--surface-2)", borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", padding: 8, minWidth: 160, textAlign: "left", verticalAlign: "top" }}>
                    <div style={{ display: "flex", flexDirection: "column", gap: 6, opacity: c.include ? 1 : 0.45 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <button onClick={() => updCol(i, { include: !c.include })} title="Include column" style={{ width: 16, height: 16, borderRadius: 4, border: `1.5px solid ${c.include ? "var(--accent)" : "var(--border-2)"}`, background: c.include ? "var(--accent)" : "transparent", color: "var(--accent-fg)", display: "flex", alignItems: "center", justifyContent: "center", padding: 0, cursor: "pointer", flexShrink: 0 }}>
                          {c.include && <Icon name="check" style={{ fontSize: 13 }} />}
                        </button>
                        <input value={c.name} onChange={(e) => updCol(i, { name: e.target.value })} className="focus-ring" style={{ flex: 1, minWidth: 0, height: 24, padding: "0 6px", borderRadius: 4, border: `1px solid ${c.name !== c.source ? "var(--accent)" : "var(--border-2)"}`, background: "var(--surface)", outline: "none", fontFamily: "'Geist Mono',monospace", fontSize: 12, fontWeight: 500 }} />
                      </div>
                      <Select value={c.type} onChange={(e) => updCol(i, { type: e.target.value })} style={{ height: 24, fontSize: 11, color: "var(--code-type)" }}>
                        <option value={AUTO_TYPE}>{AUTO_TYPE} · {c.nativeType}</option>
                        {LOGICAL_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                      </Select>
                      {c.name !== c.source && <span style={{ fontFamily: "'Geist Mono',monospace", fontSize: 10, color: "var(--text-3)" }}>was {c.source}</span>}
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {insp.preview.map((row, ri) => (
                <tr key={ri}>
                  <td style={tdNum}>{ri + 1}</td>
                  {s.columns.map((c) => {
                    const v = row[c.source];
                    return <td key={c.source} style={{ borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", padding: "0 8px", height: 28, whiteSpace: "nowrap", fontFamily: "'Geist Mono',monospace", opacity: c.include ? 1 : 0.35, color: v == null ? "var(--text-3)" : "var(--text)" }}>{v == null ? "∅" : String(v)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

const thNum: CSSProperties = { position: "sticky", top: 0, left: 0, zIndex: 3, background: "var(--surface-2)", borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", width: 44, padding: "0 8px", color: "var(--text-3)", fontWeight: 500, fontFamily: "'Geist Mono',monospace" };
const tdNum: CSSProperties = { position: "sticky", left: 0, zIndex: 1, background: "var(--surface-2)", borderBottom: "1px solid var(--border)", borderRight: "1px solid var(--border)", padding: "0 8px", height: 28, color: "var(--text-3)", fontFamily: "'Geist Mono',monospace", textAlign: "right" };

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
      <span style={{ color: "var(--text-2)" }}>{label}</span>
      <span style={{ fontFamily: "'Geist Mono',monospace" }}>{value}</span>
    </div>
  );
}

// ---- Database: transfer plan + planner recommendation ---------------------------------

const MODES: { id: Strategy; label: string }[] = [
  { id: "full", label: "Full load" }, { id: "id", label: "ID range" }, { id: "time", label: "Time range" },
];

function DatabasePlan({ s, set }: Props) {
  const schema = s.tableSchema;
  const candidates = schema?.candidates || [];
  const idCandidates = candidates.filter((c) => c.mode === "id_range");
  const timeCandidates = candidates.filter((c) => c.mode === "time_range");
  const allCols = schema?.columns.map((c) => c.name) || [];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 16, alignItems: "start" }}>
      <Card>
        <CardHeader icon="route" title="Transfer plan" right={<span style={{ fontSize: 10, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", padding: "1px 6px", borderRadius: 4, border: "1px dashed var(--border-2)", color: "var(--text-3)" }}>{schema?.exact ? "exact" : "estimated"}</span>} />
        <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 14 }}>
          <SegmentedControl options={MODES} value={s.strategy} onChange={(id) => set({ strategy: id, splitColumn: null })} />

          {s.strategy === "full" && (
            <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Split by <span style={{ fontWeight: 400, color: "var(--text-3)" }}>· optional, runs multiple splits in parallel without a value range</span></span>
              <Select value={s.fullSplitColumn || ""} onChange={(e) => set({ fullSplitColumn: e.target.value || null })}>
                <option value="">Single stream (no split)</option>
                {candidates.map((c) => <option key={c.column} value={c.column}>{c.column}</option>)}
              </Select>
            </label>
          )}

          {s.strategy === "id" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3,minmax(0,1fr))", gap: 10 }}>
                <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Column</span>
                  <Select value={s.splitColumn || ""} onChange={(e) => set({ splitColumn: e.target.value || null })}>
                    <option value="">Choose…</option>
                    {(idCandidates.length ? idCandidates.map((c) => c.column) : allCols).map((c) => <option key={c} value={c}>{c}</option>)}
                  </Select>
                </label>
                <label style={{ display: "flex", flexDirection: "column", gap: 6, opacity: s.afterWatermark ? 0.45 : 1 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>From</span>
                  <input disabled={s.afterWatermark} value={s.lo} onChange={(e) => set({ lo: e.target.value })} style={inp} />
                </label>
                <label style={{ display: "flex", flexDirection: "column", gap: 6, opacity: s.afterWatermark ? 0.45 : 1 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>To</span>
                  <input disabled={s.afterWatermark} value={s.hi} onChange={(e) => set({ hi: e.target.value })} style={inp} />
                </label>
              </div>
              <ToggleRow on={s.afterWatermark} onClick={() => set({ afterWatermark: !s.afterWatermark })} title="After last loaded value" sub="Only rows above the highest value loaded on a previous run of this exact source → target pair." />
            </>
          )}

          {s.strategy === "time" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1.4fr)", gap: 10 }}>
                <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Column</span>
                  <Select value={s.splitColumn || ""} onChange={(e) => set({ splitColumn: e.target.value || null })}>
                    <option value="">Choose…</option>
                    {(timeCandidates.length ? timeCandidates.map((c) => c.column) : allCols).map((c) => <option key={c} value={c}>{c}</option>)}
                  </Select>
                </label>
                <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Timezone</span>
                  <Select value={s.timezone} onChange={(e) => set({ timezone: e.target.value })}>
                    {TIMEZONES.map((t) => <option key={t.v} value={t.v}>{t.l}</option>)}
                  </Select>
                </label>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 10, opacity: s.lastNDays ? 0.45 : 1 }}>
                <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>From <span style={{ fontWeight: 400, color: "var(--text-3)" }}>· inclusive</span></span>
                  <input disabled={s.lastNDays} type="datetime-local" value={s.lo} onChange={(e) => set({ lo: e.target.value })} style={inp} />
                </label>
                <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>To <span style={{ fontWeight: 400, color: "var(--text-3)" }}>· exclusive</span></span>
                  <input disabled={s.lastNDays} type="datetime-local" value={s.hi} onChange={(e) => set({ hi: e.target.value })} style={inp} />
                </label>
              </div>
              <div style={{ display: "flex", gap: 6, fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text-3)" }}>
                <Icon name="schedule" style={{ fontSize: 14 }} /><span>Naive bounds are read in {s.timezone} and converted to UTC.</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <ToggleRow on={s.lastNDays} onClick={() => set({ lastNDays: !s.lastNDays })} title="" />
                <span style={{ fontWeight: 500, marginLeft: -6 }}>Last</span>
                <input value={s.days} onChange={(e) => set({ days: e.target.value })} style={{ ...inp, width: 56, textAlign: "right" }} />
                <span style={{ fontWeight: 500 }}>days</span>
                <span style={{ fontSize: 12, color: "var(--text-3)" }}>· relative to run time</span>
              </div>
              {s.splitColumn && timeCandidates.length > 0 && !timeCandidates.some((c) => c.column === s.splitColumn) && (
                <Warn text={`${s.splitColumn} is not a recommended time column: each split may scan much of the table.`} />
              )}
            </>
          )}

          <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>Extra WHERE filter <span style={{ fontWeight: 400, color: "var(--text-3)" }}>· optional, applied to every split</span></span>
            <div style={{ display: "flex", alignItems: "center", height: 32, borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", overflow: "hidden" }}>
              <span style={{ padding: "0 8px", height: "100%", display: "flex", alignItems: "center", background: "var(--surface-2)", borderRight: "1px solid var(--border)", fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--code-kw)" }}>WHERE</span>
              <input value={s.where} onChange={(e) => set({ where: e.target.value })} placeholder="status <> 'test'" style={{ flex: 1, minWidth: 0, height: "100%", padding: "0 10px", border: "none", background: "transparent", outline: "none", fontFamily: "'Geist Mono',monospace", fontSize: 12 }} />
            </div>
          </label>
        </div>
      </Card>

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Card>
          <CardHeader icon="auto_awesome" title="Planner recommendation" right={<span style={{ fontSize: 12, color: "var(--text-3)" }}>from cheap catalog metadata</span>} />
          {candidates.length === 0 && <div style={{ padding: 14, fontSize: 12, color: "var(--text-3)" }}>{schema ? "No indexed numeric or timestamp column found." : "Pick a table in step 1 first."}</div>}
          {candidates.map((c) => <CandidateRow key={c.column} c={c} active={c.column === s.splitColumn || c.column === s.fullSplitColumn} />)}
        </Card>

        <Card>
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", minHeight: 45, borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
            <Icon name="view_agenda" style={{ color: "var(--text-3)" }} /><span style={{ fontWeight: 600, flex: 1 }}>Chunk size</span>
            <SegmentedControl small options={[{ id: "rec", label: "Recommended" }, { id: "custom", label: "Custom" }]} value={s.chunkMode} onChange={(id) => set({ chunkMode: id })} />
          </div>
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 12 }}>
            {s.chunkMode === "rec" ? (
              <div style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "10px 12px", borderRadius: 6, background: "var(--accent-soft)" }}>
                <Icon name="auto_awesome" style={{ fontSize: 16, color: "var(--accent)" }} />
                <div>
                  <div style={{ fontFamily: "'Geist Mono',monospace", fontSize: 14, fontWeight: 500 }}>50,000 <span style={{ fontFamily: "'Geist',sans-serif", fontSize: 12, fontWeight: 400, color: "var(--text-2)" }}>rows per batch</span></div>
                  <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 2 }}>Default read/write batch size — one commit per batch per worker.</div>
                </div>
              </div>
            ) : (
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <div style={{ display: "flex", alignItems: "center", height: 32, borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", overflow: "hidden", width: 180 }}>
                  <input type="number" value={s.chunk} onChange={(e) => set({ chunk: Math.max(100, Number(e.target.value)) })} style={{ flex: 1, minWidth: 0, height: "100%", padding: "0 10px", border: "none", background: "transparent", outline: "none", fontFamily: "'Geist Mono',monospace", fontSize: 13, textAlign: "right" }} />
                  <span style={{ padding: "0 10px", height: "100%", display: "flex", alignItems: "center", background: "var(--surface-2)", borderLeft: "1px solid var(--border)", fontSize: 12, color: "var(--text-3)" }}>rows</span>
                </div>
                {[10000, 25000, 100000, 250000].map((n) => (
                  <button key={n} onClick={() => set({ chunk: n })} style={{ height: 28, padding: "0 10px", borderRadius: 6, border: `1px solid ${s.chunk === n ? "var(--accent)" : "var(--border-2)"}`, background: "var(--surface)", color: s.chunk === n ? "var(--accent)" : "var(--text-2)", fontFamily: "'Geist Mono',monospace", fontSize: 12, cursor: "pointer" }}>{fmtN(n)}</button>
                ))}
              </div>
            )}
            {s.chunkMode === "custom" && s.chunk < 5000 && <Warn text="Small batches add commit overhead — throughput may drop." />}
            {s.chunkMode === "custom" && s.chunk > 500000 && <Warn text="Large batches raise memory use and retry cost." />}
          </div>
        </Card>

        <Card>
          <CardHeader icon="lan" title="Parallelism" />
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <button onClick={() => set({ workers: Math.max(1, s.workers - 1) })} style={btnSquare}><Icon name="remove" style={{ fontSize: 17 }} /></button>
              <input type="range" min={1} max={32} step={1} value={s.workers} onChange={(e) => set({ workers: Number(e.target.value) })} style={{ flex: 1, accentColor: "var(--accent)" }} />
              <button onClick={() => set({ workers: Math.min(32, s.workers + 1) })} style={btnSquare}><Icon name="add" style={{ fontSize: 17 }} /></button>
              <span style={{ minWidth: 28, textAlign: "right", fontFamily: "'Geist Mono',monospace", fontSize: 14, fontWeight: 500 }}>{s.workers}</span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}

const inp: CSSProperties = { height: 32, padding: "0 10px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", outline: "none", fontFamily: "'Geist Mono',monospace", fontSize: 12 };
const btnSquare: CSSProperties = { width: 30, height: 30, borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer" };

function Warn({ text }: { text: string }) {
  return (
    <div style={{ display: "flex", gap: 8, padding: "10px 12px", borderRadius: 6, background: "var(--warn-soft)", fontSize: 12, color: "var(--text)" }}>
      <Icon name="warning" style={{ fontSize: 15, color: "var(--warn)" }} />{text}
    </div>
  );
}

function CandidateRow({ c, active }: { c: SplitCandidateOut; active: boolean }) {
  const TAG: Record<string, [string, string, string]> = {
    recommended: ["Recommended", "var(--accent-fg)", "var(--accent)"],
    alternative: ["Alternative", "var(--text-2)", "var(--surface-3)"],
    not_advised: ["Not advised", "var(--warn)", "var(--warn-soft)"],
  };
  const [label, fg, bg] = TAG[c.verdict];
  return (
    <div style={{ display: "flex", gap: 12, padding: 14, borderBottom: "1px solid var(--border)", boxShadow: active ? "inset 3px 0 0 var(--accent)" : undefined, background: active ? "var(--accent-soft)" : undefined }}>
      <span style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        <span style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontWeight: 600 }}>{c.column}</span>
          <span style={{ fontSize: 10, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", padding: "2px 6px", borderRadius: 4, background: bg, color: fg }}>{label}</span>
          <span style={{ fontSize: 11, color: "var(--text-3)", fontFamily: "'Geist Mono',monospace" }}>{c.mode.replace("_", " ")}</span>
        </span>
        <span style={{ fontSize: 12, color: "var(--text-2)" }}>{c.reasons.join(" · ")}</span>
        {c.warnings.map((w, i) => <Warn key={i} text={w} />)}
      </span>
    </div>
  );
}
