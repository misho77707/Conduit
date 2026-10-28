import type { ConnectionOut, DbTypeOut, DriverChoice, JobPlan, TransferMethod, TransferMethodInfo } from "../../lib/api";
import { Field } from "../../ui/Field";
import { Select } from "../../ui/Input";
import { SegmentedControl } from "../../ui/SegmentedControl";

/** How a job's rows move: per-side driver overrides (null = the connection's own setting) and the transfer method. */
export interface Movement {
  source_driver: string | null;
  target_driver: string | null;
  transfer_method: TransferMethod | null;
}

export const NO_MOVEMENT: Movement = { source_driver: null, target_driver: null, transfer_method: null };

type Side = "source" | "target";

function settingLabel(conn: ConnectionOut | undefined, type: DbTypeOut | undefined): string {
  if (!conn?.driver || conn.driver === "auto") return "automatic";
  return type?.drivers.find((o) => o.id === conn.driver)?.label || conn.driver;
}

/**
 * The wizard's and the pipeline builder's controls. `method`: the plan's transfer-method info (null: rows can only
 * move through Conduit, so no control); `drivers`: what the last plan chose per side. A side shows a driver
 * choice only when its database has one.
 */
export function MovementEditor({ value, onChange, method, drivers, sourceConn, targetConn, dbTypes }: {
  value: Movement; onChange: (v: Movement) => void; method: TransferMethodInfo | null | undefined;
  drivers?: JobPlan["drivers"]; sourceConn?: ConnectionOut; targetConn?: ConnectionOut; dbTypes: Record<string, DbTypeOut>;
}) {
  const pull = value.transfer_method === "clickhouse_pull";
  const sides: { side: Side; conn?: ConnectionOut; key: "source_driver" | "target_driver" }[] = [
    { side: "source", conn: sourceConn, key: "source_driver" }, { side: "target", conn: targetConn, key: "target_driver" },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {method && (
        <Field label="Transfer method">
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {(method.eligible || pull) && (
              <div>
                <SegmentedControl small value={pull ? "clickhouse_pull" : "conduit"}
                  onChange={(id) => onChange({ ...value, transfer_method: id === "conduit" ? null : id })}
                  options={[{ id: "conduit" as TransferMethod, label: "Through Conduit" }, { id: "clickhouse_pull" as TransferMethod, label: "ClickHouse pulls directly" }]} />
              </div>
            )}
            {!method.eligible && (
              <span style={{ fontSize: 12, color: pull ? "var(--err)" : "var(--text-3)" }}>ClickHouse can&rsquo;t pull this directly: {method.why}</span>
            )}
            {pull && method.eligible && (
              <div style={{ fontSize: 12, color: "var(--text-2)", padding: "8px 10px", borderRadius: 6, background: "var(--surface-2)", display: "flex", flexDirection: "column", gap: 4 }}>
                <span>ClickHouse reads the source itself through the named collection <code>{method.collection}</code>: no row passes through Conduit.</span>
                {method.limits.map((l) => <span key={l} style={{ color: "var(--text-3)" }}>· {l}</span>)}
              </div>
            )}
          </div>
        </Field>
      )}
      {!pull && sides.map(({ side, conn, key }) => {
        const type = conn ? dbTypes[conn.type] : undefined;
        if (!type?.drivers.length) return null;
        const used = drivers?.[side];
        return (
          <Field key={side} label={`${side === "source" ? "Source" : "Target"} driver`}>
            <Select value={value[key] ?? ""} onChange={(e) => onChange({ ...value, [key]: e.target.value || null })}>
              <option value="">Connection&rsquo;s setting ({settingLabel(conn, type)})</option>
              <option value="auto">Automatic: the fastest that can run this job</option>
              {type.drivers.filter((o) => (side === "source" ? o.read : o.write) || o.classic).map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </Select>
            {used && <DriverUsed choice={used} />}
          </Field>
        );
      })}
    </div>
  );
}

function DriverUsed({ choice }: { choice: DriverChoice }) {
  return (
    <span style={{ fontSize: 11, color: choice.why ? "var(--warn)" : "var(--text-3)" }}>
      Uses {choice.label}{choice.why ? `: ${choice.why.replace(/^(source|target): /, "")}` : ""}
    </span>
  );
}

/** Job detail: what moved the rows, and why a faster driver wasn't used. */
export function MovementSummary({ plan }: { plan: Partial<JobPlan> | null | undefined }) {
  if (!plan) return null;
  const method = plan.transfer_method;
  const rows: { k: string; v: string; why?: string | null }[] = [];
  if (method?.used === "clickhouse_pull") {
    rows.push({ k: "Transfer method", v: `ClickHouse pulled directly through \`${method.collection}\`` });
  } else {
    for (const side of ["source", "target"] as Side[]) {
      const c = plan.drivers?.[side];
      if (c) rows.push({ k: `${side === "source" ? "Source" : "Target"} driver`, v: c.label, why: c.why?.replace(/^(source|target): /, "") });
    }
  }
  if (!rows.length) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {rows.map((r) => (
        <div key={r.k} style={{ display: "flex", gap: 8, fontSize: 12, flexWrap: "wrap" }}>
          <span style={{ color: "var(--text-3)", minWidth: 110 }}>{r.k}</span>
          <span style={{ fontWeight: 500 }}>{r.v}</span>
          {r.why && <span style={{ color: "var(--warn)" }}>{r.why}</span>}
        </div>
      ))}
    </div>
  );
}
