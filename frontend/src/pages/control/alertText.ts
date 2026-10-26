// What each alert event means, in words, and the one-sentence preview of a rule. Mirrors backend/app/alerts/catalog.py.
import type { AlertChannelOut, AlertEvent, AlertKind, AlertRuleIn } from "../../lib/api";

export type ScopeKey = "pipelines" | "connections" | "envs" | "checks";

export const EVENTS: Record<AlertEvent, { label: string; help: string; scope: ScopeKey[]; defaults: Record<string, unknown> }> = {
  job_failed: {
    label: "Job failed", scope: ["pipelines"], defaults: { jobs: "all" },
    help: "A load or pipeline run ends failed. A pipeline's alert clears when its next run succeeds; a failed one-off load sends one message.",
  },
  job_stalled: {
    label: "Job stalled", scope: ["pipelines"], defaults: {},
    help: "A running job made no progress for 90 s (its worker probably stopped). Clears when it moves again or ends.",
  },
  schedule_failed: {
    label: "Schedule couldn't start", scope: ["pipelines"], defaults: {},
    help: "The scheduler could not launch a due pipeline: no cached preview, a broken config, the job queue down. Clears when the next launch works.",
  },
  input_stale: {
    label: "Pipeline input stale", scope: ["pipelines"], defaults: { hours: 24 },
    help: "A scheduled pipeline that loads the newest / all remote files (skipping loaded ones) got no new file for N hours.",
  },
  connection_down: {
    label: "Connection down", scope: ["connections", "envs"], defaults: { k: 2, include_warn: false },
    help: "A connection's health check failed K times in a row. Clears when it is back to OK.",
  },
  check_down: {
    label: "Service check down", scope: ["checks"], defaults: { k: 2 },
    help: "A service check was down K runs in a row. Clears when it is up.",
  },
  host_high: {
    label: "Host resource high", scope: [], defaults: { resources: ["disk", "memory", "cpu"], pct: null, minutes: 5 },
    help: "Disk, memory or CPU stays above a threshold for M minutes. Clears when it drops below.",
  },
  compare_diff: {
    label: "Compare found differences", scope: [], defaults: {},
    help: "A comparison finishes with missing, extra, different or duplicate rows. One message per comparison.",
  },
};

export const EVENT_ORDER = Object.keys(EVENTS) as AlertEvent[];

export const KIND: Record<AlertKind, { label: string; icon: string }> = {
  telegram: { label: "Telegram", icon: "send" },
  slack: { label: "Slack", icon: "tag" },
  email: { label: "Email", icon: "mail" },
  webhook: { label: "Webhook", icon: "webhook" },
};

const times = (n: number, what: string) => `${n} ${what}${n === 1 ? "" : "s"}`;

export function listing(names: string[]): string {
  if (names.length <= 1) return names[0] || "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** "chat -1001234", "a@x.com via smtp.x.com:587" ... (never a secret) */
export function channelTarget(c: Pick<AlertChannelOut, "kind" | "config" | "secrets_set">): string {
  const cfg = c.config as Record<string, unknown>;
  switch (c.kind) {
    case "telegram": return `chat ${cfg.chat_id || "?"}`;
    case "slack": return "Slack incoming webhook";
    case "email": return `${((cfg.to as string[]) || []).join(", ")} via ${cfg.host}:${cfg.port}`;
    case "webhook": return `POST to the saved URL${c.secrets_set.includes("signing_secret") ? " · signed" : ""}`;
  }
}

export interface Names { channels: Map<string, string>; pipelines: Map<string, string>; connections: Map<string, string>; checks: Map<string, string>; }

/** "When a scheduled pipeline run fails, send to Telegram and #data-alerts." */
export function describeRule(r: Pick<AlertRuleIn, "event" | "scope" | "options" | "channel_ids">, n: Names): string {
  const o = r.options as Record<string, any>;
  const picked = (key: ScopeKey, map?: Map<string, string>) => ((r.scope as any)[key] || []).map((id: string) => (map ? map.get(id) || "a deleted one" : `env ${id}`));
  const scope = r.scope.all ? [] : [...picked("pipelines", n.pipelines), ...picked("connections", n.connections), ...picked("envs"), ...picked("checks", n.checks)];
  const of = scope.length ? ` (${listing(scope)})` : "";
  let when: string;
  switch (r.event) {
    case "job_failed": when = `When ${o.jobs === "scheduled" ? "a scheduled pipeline run" : o.jobs === "pipelines" ? "a pipeline run" : "a load or pipeline run"}${of} fails`; break;
    case "job_stalled": when = `When a job${of} stalls`; break;
    case "schedule_failed": when = `When the schedule can't start a pipeline${of}`; break;
    case "input_stale": when = `When a pipeline${of} gets no new file for ${o.hours ?? 24} h`; break;
    case "connection_down": when = `When a connection${of} fails ${times(o.k ?? 2, "health check")} in a row${o.include_warn ? " (degraded counts)" : ""}`; break;
    case "check_down": when = `When a service check${of} is down ${times(o.k ?? 2, "run")} in a row`; break;
    case "host_high": {
      const res = ((o.resources as string[]) || []).map((x) => (x === "cpu" ? "CPU" : x));
      const level = o.pct ? `${o.pct}%` : "the Monitor's warning level";
      const minutes = o.minutes ?? 5;
      when = `When ${listing(res) || "nothing"} ${minutes ? `stays above ${level} for ${minutes} min` : `goes above ${level}`}`;
      break;
    }
    case "compare_diff": when = "When a comparison finds differences"; break;
  }
  const to = r.channel_ids.map((id) => n.channels.get(id) || "a deleted channel");
  return `${when}, ${to.length ? `send to ${listing(to)}` : "send nowhere yet (pick a channel)"}.`;
}
