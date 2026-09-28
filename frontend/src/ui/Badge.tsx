import { Icon } from "./Icon";
import type { Badge as BadgeT } from "../lib/format";

export function StatusBadge({ b, size = "md" }: { b: BadgeT; size?: "sm" | "md" }) {
  const h = size === "sm" ? 20 : 22;
  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: 5, height: h, padding: "0 8px 0 6px",
        borderRadius: 5, fontSize: size === "sm" ? 11 : 12, fontWeight: 500, color: b.fg, background: b.bg,
      }}
    >
      <Icon name={b.icon} spin={b.spin} style={{ fontSize: size === "sm" ? 14 : 15 }} />
      {b.label}
    </span>
  );
}
