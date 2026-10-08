import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  compareApi, connectionsApi, jobsApi, type CompareColumnsOut, type CompareIn, type CompareOptions, type CompareOut,
  type ComparePair, type ConnectionOut, type SourceIn,
} from "../../lib/api";
import { useToast } from "../../lib/toast";
import { Icon } from "../../ui/Icon";
import { Button } from "../../ui/Button";
import { Card, CardHeader, CardBody } from "../../ui/Card";
import { Input, Select } from "../../ui/Input";
import { ToggleRow } from "../../ui/Toggle";
import { SourceEditor } from "../pipelines/SourceTargetEditor";

export const DEFAULT_OPTIONS: CompareOptions = {
  ignore_nulls: false, check_duplicates: true, trim_text: false, case_insensitive: false, float_decimals: 6, sample_rows: 100,
};

const emptySide = (): SourceIn => ({ connection_id: null, table: "", where: "" });
const ready = (s: SourceIn) => (s.remote ? !!s.remote.path.trim() : s.file ? !!s.file.path?.trim() : !!s.connection_id && !!(s.table?.trim() || s.query?.trim()));

interface Row { source: string; sourceType: string; target: string; include: boolean }
interface Preset { pairs: ComparePair[] | null; keys: string[] | null }

export interface SetupState { fromJobId?: string; fromSpec?: CompareIn }

export function CompareSetup({ onStarted }: { onStarted: (c: CompareOut) => void }) {
  const loc = useLocation() as { state?: SetupState | null };
  const toast = useToast();
  const [conns, setConns] = useState<ConnectionOut[]>([]);
  const [name, setName] = useState("");
  const [source, setSource] = useState<SourceIn>(emptySide);
  const [target, setTarget] = useState<SourceIn>(emptySide);
  const [jobId, setJobId] = useState<string | null>(null);
  const [cols, setCols] = useState<CompareColumnsOut | null>(null);
  const [colsError, setColsError] = useState<string | null>(null);
  const [colsLoading, setColsLoading] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);
  const [keys, setKeys] = useState<string[]>([]);
  const [options, setOptions] = useState<CompareOptions>(DEFAULT_OPTIONS);
  const [starting, setStarting] = useState(false);
  const preset = useRef<Preset | null>(null); // pairing / keys to apply once the columns are known (Verify, Edit)

  useEffect(() => { connectionsApi.list().then(setConns).catch(() => {}); }, []);

  // pre-fill from a job ("Verify") or an earlier comparison ("Edit")
  useEffect(() => {
    const st = loc.state;
    if (st?.fromSpec) {
      const sp = st.fromSpec;
      setName(sp.name || ""); setSource(sp.source); setTarget(sp.target);
      setOptions({ ...DEFAULT_OPTIONS, ...sp.options }); setJobId(sp.job_id || null);
      preset.current = { pairs: sp.columns ?? null, keys: sp.keys ?? null };
    } else if (st?.fromJobId) {
      jobsApi.get(st.fromJobId).then((job) => {
        const sp = job.spec;
        if (!sp) throw new Error("This job has no saved request");
        setName(`Verify ${job.name}`);
        setSource(sp.source);
        setTarget({ connection_id: sp.target.connection_id, table: sp.target.table, where: "" });
        setJobId(job.id);
        preset.current = {
          pairs: sp.columns ? sp.columns.map((c) => ({ source: c.source, target: c.name || c.source, include: c.include })) : null,
          keys: sp.target.mode === "upsert" && sp.target.keys.length ? sp.target.keys : null,
        };
      }).catch((e) => toast("err", "Could not open the job", e instanceof Error ? e.message : undefined));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc.state]);

  // learn both sides' columns (debounced) once both are filled in
  useEffect(() => {
    if (!ready(source) || !ready(target)) { setCols(null); setRows([]); return; }
    const t = setTimeout(() => {
      setColsLoading(true); setColsError(null);
      compareApi.columns(source, target).then((res) => {
        setCols(res);
        const tgtNames = new Set(res.target_columns.map((c) => c.name));
        const pre = preset.current;
        setRows(res.source_columns.map((sc) => {
          const p = pre?.pairs?.find((x) => x.source === sc.name);
          if (p && tgtNames.has(p.target)) return { source: sc.name, sourceType: sc.type, target: p.target, include: p.include };
          const auto = res.pairs.find((x) => x.source === sc.name);
          // with a preset pairing, columns it left out stay out (the job did not load them)
          return { source: sc.name, sourceType: sc.type, target: auto?.target || "", include: !!auto && !pre?.pairs };
        }));
        setKeys(pre?.keys?.filter((k) => tgtNames.has(k)) ?? res.suggested_keys);
        preset.current = null;
      }).catch((e) => { setCols(null); setRows([]); setColsError(e instanceof Error ? e.message : "Could not read the columns"); })
        .finally(() => setColsLoading(false));
    }, 600);
    return () => clearTimeout(t);
  }, [JSON.stringify(source), JSON.stringify(target)]);

  const tgtType = useMemo(() => Object.fromEntries((cols?.target_columns || []).map((c) => [c.name, c.type])), [cols]);
  const included = rows.filter((r) => r.include && r.target);
  const usedTargets = new Set(included.map((r) => r.target));
  const dupTarget = included.find((r, i) => included.findIndex((x) => x.target === r.target) !== i);
  const validKeys = keys.filter((k) => usedTargets.has(k));
  const onlyInTarget = (cols?.target_columns || []).filter((c) => !usedTargets.has(c.name)).map((c) => c.name);

  const upd = (i: number, patch: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const toggleKey = (k: string) => setKeys((ks) => (ks.includes(k) ? ks.filter((x) => x !== k) : [...ks, k]));

  const problem = !ready(source) ? "Choose the source" : !ready(target) ? "Choose the target"
    : colsError ? "Fix the error above" : !included.length ? "Include at least one column pair"
    : dupTarget ? `Target column ${dupTarget.target} is paired twice` : null;

  async function run() {
    if (problem) return;
    setStarting(true);
    try {
      const body: CompareIn = {
        name: name.trim() || null, source, target, options, job_id: jobId,
        columns: included.map((r) => ({ source: r.source, target: r.target, include: true })),
        keys: validKeys,
      };
      onStarted(await compareApi.create(body));
    } catch (e) {
      toast("err", "Could not start the comparison", e instanceof Error ? e.message : undefined);
    } finally {
      setStarting(false);
    }
  }

  type Flag = "ignore_nulls" | "check_duplicates" | "trim_text" | "case_insensitive";
  const opt = (k: Flag) => () => setOptions((o) => ({ ...o, [k]: !o[k] }));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))", gap: 16 }}>
        <Card><CardHeader icon="input" title="Source" right={<span style={{ fontSize: 12, color: "var(--text-3)" }}>what should be there</span>} /><CardBody><SourceEditor value={source} onChange={setSource} conns={conns} /></CardBody></Card>
        <Card><CardHeader icon="output" title="Target" right={<span style={{ fontSize: 12, color: "var(--text-3)" }}>what is there</span>} /><CardBody><SourceEditor value={target} onChange={setTarget} conns={conns} /></CardBody></Card>
      </div>

      <Card>
        <CardHeader icon="view_column" title="Columns" right={<>
          {colsLoading && <Icon name="progress_activity" spin style={{ fontSize: 16, color: "var(--text-3)" }} />}
          {cols && <span style={{ fontSize: 12, color: "var(--text-3)" }}>{included.length} of {rows.length} compared</span>}
          {cols && <Button height={26} padX={8} style={{ fontSize: 12 }} onClick={() => setRows((rs) => rs.map((r) => ({ ...r, include: !!r.target })))}>All paired</Button>}
          {cols && <Button height={26} padX={8} style={{ fontSize: 12 }} onClick={() => setRows((rs) => rs.map((r) => ({ ...r, include: false })))}>None</Button>}
        </>} />
        <CardBody>
          {!cols && !colsError && <div style={{ fontSize: 12, color: "var(--text-3)" }}>Choose both sides to see their columns. Same-named columns are paired automatically.</div>}
          {colsError && <div style={{ display: "flex", gap: 8, color: "var(--err)", fontSize: 12 }}><Icon name="error" style={{ fontSize: 16 }} />{colsError}</div>}
          {cols && (
            <>
              <div style={{ border: "1px solid var(--border)", borderRadius: 6, overflow: "auto" }}>
                <div style={{ display: "grid", gridTemplateColumns: "28px 34px minmax(160px,1fr) 20px minmax(180px,1fr) 110px", gap: 8, alignItems: "center", padding: "0 10px", height: 30, fontSize: 11, fontWeight: 500, color: "var(--text-3)", borderBottom: "1px solid var(--border)", minWidth: 560 }}>
                  <span /><span>Key</span><span>Source column</span><span /><span>Target column</span><span />
                </div>
                {rows.map((r, i) => {
                  const isKey = validKeys.includes(r.target) && r.include;
                  return (
                    <div key={r.source} style={{ display: "grid", gridTemplateColumns: "28px 34px minmax(160px,1fr) 20px minmax(180px,1fr) 110px", gap: 8, alignItems: "center", padding: "4px 10px", borderBottom: "1px solid var(--border)", minWidth: 560, opacity: r.include ? 1 : 0.55 }}>
                      <input type="checkbox" checked={r.include} disabled={!r.target} onChange={() => upd(i, { include: !r.include })} />
                      <button title={isKey ? "Matched on this column" : "Use as a key column"} disabled={!r.include || !r.target} onClick={() => toggleKey(r.target)}
                        style={{ width: 26, height: 24, borderRadius: 5, border: `1px solid ${isKey ? "var(--accent)" : "var(--border-2)"}`, background: isKey ? "var(--accent-soft)" : "transparent", color: isKey ? "var(--accent)" : "var(--text-3)", cursor: r.include ? "pointer" : "default", display: "flex", alignItems: "center", justifyContent: "center", padding: 0 }}>
                        <Icon name="key" style={{ fontSize: 14 }} />
                      </button>
                      <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "'Geist Mono',monospace", fontSize: 12 }}>
                        {r.source} <span style={{ color: "var(--code-type)", fontSize: 10 }}>{r.sourceType}</span>
                      </span>
                      <Icon name="arrow_forward" style={{ fontSize: 15, color: "var(--text-3)" }} />
                      <Select value={r.target} onChange={(e) => upd(i, { target: e.target.value, include: !!e.target.value })} style={{ height: 28 }}>
                        <option value="">— not compared —</option>
                        {cols.target_columns.map((c) => <option key={c.name} value={c.name}>{c.name} · {c.type}</option>)}
                      </Select>
                      <span style={{ fontSize: 11, color: !r.target ? "var(--text-3)" : r.target.toLowerCase() === r.source.toLowerCase() ? "var(--text-3)" : "var(--accent)" }}>
                        {!r.target ? "no match" : r.target.toLowerCase() === r.source.toLowerCase() ? "same name" : "renamed"}
                        {r.target && tgtType[r.target] && tgtType[r.target] !== r.sourceType ? " · types differ" : ""}
                      </span>
                    </div>
                  );
                })}
              </div>
              {onlyInTarget.length > 0 && <div style={{ fontSize: 12, color: "var(--text-3)" }}>Only in the target (not compared): <span style={{ fontFamily: "'Geist Mono',monospace" }}>{onlyInTarget.join(", ")}</span></div>}
              <div style={{ display: "flex", gap: 8, padding: "10px 12px", borderRadius: 6, background: validKeys.length ? "var(--accent-soft)" : "var(--warn-soft)", fontSize: 12, alignItems: "flex-start" }}>
                <Icon name={validKeys.length ? "key" : "info"} style={{ fontSize: 16, color: validKeys.length ? "var(--accent)" : "var(--warn)" }} />
                <span style={{ color: "var(--text)" }}>
                  {validKeys.length
                    ? <>Rows are matched by <b style={{ fontFamily: "'Geist Mono',monospace" }}>{validKeys.join(", ")}</b>{cols.suggested_keys.join() === validKeys.join() ? ` (${cols.key_note})` : ""}. You get rows missing in the target, extra rows, and rows whose values differ.</>
                    : <>No key column: whole rows are compared, so a changed row shows up as one missing and one extra row. Click <Icon name="key" style={{ fontSize: 13, verticalAlign: "middle" }} /> next to an identifying column to match by key.</>}
                </span>
              </div>
            </>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader icon="tune" title="Options" right={<Button height={26} padX={8} style={{ fontSize: 12 }} onClick={() => setOptions(DEFAULT_OPTIONS)}>Defaults</Button>} />
        <CardBody>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(260px,1fr))", gap: 14 }}>
            <div style={{ opacity: validKeys.length ? 1 : 0.5 }}>
              <ToggleRow on={options.ignore_nulls && !!validKeys.length} onClick={() => validKeys.length && opt("ignore_nulls")()} title="Ignore nulls"
                sub={validKeys.length ? "An empty (NULL) value on either side is not counted as a difference." : "Needs a key column."} />
            </div>
            <ToggleRow on={options.check_duplicates} onClick={opt("check_duplicates")} title="Check for duplicates" sub={validKeys.length ? "Keys that occur more than once on a side." : "Rows that occur more than once on a side."} />
            <ToggleRow on={options.trim_text} onClick={opt("trim_text")} title="Trim text" sub="Ignore leading / trailing spaces." />
            <ToggleRow on={options.case_insensitive} onClick={opt("case_insensitive")} title="Ignore upper / lower case" sub="'ABC' equals 'abc'." />
            <label style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <input type="number" min={0} max={15} value={options.float_decimals} onChange={(e) => setOptions({ ...options, float_decimals: Math.min(15, Math.max(0, Number(e.target.value) || 0)) })} style={{ width: 56, height: 28, padding: "0 8px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", fontFamily: "'Geist Mono',monospace" }} />
              <span><span style={{ fontWeight: 500 }}>Decimal places for floats</span><br /><span style={{ fontSize: 12, color: "var(--text-3)" }}>FLOAT / DOUBLE are rounded first, so 0.1 + 0.2 equals 0.3.</span></span>
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <input type="number" min={1} max={10000} value={options.sample_rows} onChange={(e) => setOptions({ ...options, sample_rows: Math.min(10000, Math.max(1, Number(e.target.value) || 100)) })} style={{ width: 72, height: 28, padding: "0 8px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", fontFamily: "'Geist Mono',monospace" }} />
              <span><span style={{ fontWeight: 500 }}>Sample size</span><br /><span style={{ fontSize: 12, color: "var(--text-3)" }}>Rows per result for a &ldquo;sample only&rdquo; export.</span></span>
            </label>
          </div>
        </CardBody>
      </Card>

      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Input mono={false} value={name} onChange={(e) => setName(e.target.value)} placeholder="Name (optional)" style={{ maxWidth: 320 }} />
        <div style={{ flex: 1 }} />
        {problem && <span style={{ fontSize: 12, color: "var(--text-3)" }}>{problem}</span>}
        <Button variant="primary" icon="difference" disabled={!!problem || starting} onClick={run}>{starting ? "Starting…" : "Compare"}</Button>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-3)" }}>
        Both sides are copied once into a scratch DuckDB file under the data folder (deleted after the diff), so a very large table needs that much free disk: use the WHERE filters to compare a slice. Each side is read as it is at that moment.
      </div>
    </div>
  );
}
