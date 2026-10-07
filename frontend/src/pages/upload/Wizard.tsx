import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { connectionsApi, jobsApi, metaApi, type ConnectionOut, type DbTypeOut } from "../../lib/api";
import { useToast } from "../../lib/toast";
import { useTheme } from "../../lib/theme";
import { Icon } from "../../ui/Icon";
import { Button } from "../../ui/Button";
import { initialWizardState, type WizardState } from "./types";
import { Step1Source } from "./Step1Source";
import { Step2Preview } from "./Step2Preview";
import { Step3Target } from "./Step3Target";
import { Step4Review } from "./Step4Review";
import { stateFromJob } from "./fromJob";

export interface WizardCtx {
  conns: ConnectionOut[];
  dbTypes: Record<string, DbTypeOut>;
  dark: boolean;
  reloadConns: () => void;
}

const STEPS = [
  { n: 1, label: "Source", sub: "File or database" },
  { n: 2, label: "Preview & plan", sub: "Shape and split" },
  { n: 3, label: "Target & options", sub: "Where it lands" },
  { n: 4, label: "Review & run", sub: "Confirm and go" },
];

export function Wizard() {
  const nav = useNavigate();
  const loc = useLocation() as { state?: { targetConnId?: string; fromJobId?: string } };
  const toast = useToast();
  const { theme } = useTheme();
  const [conns, setConns] = useState<ConnectionOut[]>([]);
  const [dbTypes, setDbTypes] = useState<Record<string, DbTypeOut>>({});
  const [s, setS] = useState<WizardState>(() => initialWizardState(loc.state?.targetConnId || null));
  const [maxStepReached, setMaxStepReached] = useState(1);
  const fromJobId = loc.state?.fromJobId;
  const [hydrating, setHydrating] = useState(!!fromJobId);

  const reloadConns = () => connectionsApi.list().then(setConns).catch(() => {});
  useEffect(() => {
    reloadConns();
    metaApi.dbTypes().then((ts) => setDbTypes(Object.fromEntries(ts.map((t) => [t.type, t]))));
  }, []);

  useEffect(() => {
    if (!fromJobId) return;
    setHydrating(true);
    jobsApi.get(fromJobId)
      .then((job) => {
        if (!job.spec) throw new Error("This job has no saved request to edit");
        return stateFromJob(job.spec);
      })
      .then((st) => { setS({ ...st, step: 4 }); setMaxStepReached(4); })
      .catch((e) => toast("err", "Could not reopen the job", e instanceof Error ? e.message : undefined))
      .finally(() => setHydrating(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromJobId]);

  const set = (patch: Partial<WizardState>) => setS((prev) => ({ ...prev, ...patch }));
  const ctx: WizardCtx = { conns, dbTypes, dark: theme === "dark", reloadConns };

  const canNext = useMemo(() => {
    if (s.step === 1) {
      if (s.filePath) return true;
      if (s.sourceTab === "database") return s.sourceView === "query" ? !!s.sourceQuery.trim() : !!s.sourceTable;
      return false;
    }
    if (s.step === 3) return !!s.targetConnId && !!s.targetTable.trim();
    return true;
  }, [s]);

  function goto(n: 1 | 2 | 3 | 4) {
    if (n > maxStepReached && !canNext) return;
    setMaxStepReached((m) => Math.max(m, n));
    set({ step: n });
  }

  async function created(id: string) {
    toast("info", "Load started");
    nav(`/jobs/${id}`);
  }

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1440 }}>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Upload &amp; Load</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>
            {fromJobId ? "Reopened from an earlier job: change anything, then run it again." : "Bring a file or table in, check its shape, and load it into any saved connection."}
          </p>
        </div>
      </div>

      {hydrating ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 24, color: "var(--text-3)" }}>
          <Icon name="progress_activity" spin style={{ fontSize: 18 }} />Loading the job's settings…
        </div>
      ) : (<>

      <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, display: "flex", alignItems: "center", gap: 14, padding: "14px 18px", overflowX: "auto" }}>
        {STEPS.map((st, i) => {
          const done = st.n < s.step || (st.n <= maxStepReached && st.n < s.step);
          const active = st.n === s.step;
          const clickable = st.n <= maxStepReached;
          return (
            <div key={st.n} style={{ display: "flex", alignItems: "center", gap: 14, flex: i < STEPS.length - 1 ? 1 : undefined }}>
              <button
                onClick={() => clickable && goto(st.n as 1 | 2 | 3 | 4)}
                style={{ display: "flex", alignItems: "center", gap: 10, background: "none", border: "none", padding: 0, cursor: clickable ? "pointer" : "default", textAlign: "left", flexShrink: 0 }}
              >
                <span style={{
                  width: 24, height: 24, borderRadius: "50%", border: `1.5px solid ${done || active ? "var(--accent)" : "var(--border-2)"}`,
                  background: done ? "var(--accent)" : active ? "var(--accent-soft)" : "var(--surface)",
                  color: done ? "var(--accent-fg)" : active ? "var(--accent)" : "var(--text-3)",
                  display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, fontWeight: 600,
                }}>
                  {done ? <Icon name="check" style={{ fontSize: 15 }} /> : st.n}
                </span>
                <span style={{ display: "flex", flexDirection: "column" }}>
                  <span style={{ fontWeight: 500, color: done || active ? "var(--text)" : "var(--text-3)" }}>{st.label}</span>
                  <span style={{ fontSize: 12, color: "var(--text-3)" }}>{st.sub}</span>
                </span>
              </button>
              {i < STEPS.length - 1 && <span style={{ flex: 1, height: 1.5, minWidth: 20, background: st.n < s.step ? "var(--accent)" : "var(--border)" }} />}
            </div>
          );
        })}
      </div>

      {s.step === 1 && <Step1Source s={s} set={set} ctx={ctx} />}
      {s.step === 2 && <Step2Preview s={s} set={set} ctx={ctx} />}
      {s.step === 3 && <Step3Target s={s} set={set} ctx={ctx} />}
      {s.step === 4 && <Step4Review s={s} set={set} ctx={ctx} onCreated={created} />}

      {s.step < 4 && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, paddingTop: 4 }}>
          <Button icon="arrow_back" disabled={s.step === 1} onClick={() => goto((s.step - 1) as 1 | 2 | 3)}>Back</Button>
          <div style={{ flex: 1 }} />
          <Button variant="primary" disabled={!canNext} onClick={() => goto((s.step + 1) as 2 | 3 | 4)} style={{ flexDirection: "row-reverse" }}>
            <Icon name="arrow_forward" style={{ fontSize: 17 }} />
            {["", "Preview & plan", "Target & options", "Review"][s.step]}
          </Button>
        </div>
      )}
      </>)}
    </div>
  );
}
