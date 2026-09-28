import type { CSSProperties } from "react";

export function Logo({ size = 22 }: { size?: number }) {
  const pad = size >= 28 ? 6 : 5;
  const gap = size >= 28 ? 3 : 2;
  const border = size >= 28 ? "1.5px" : "1.2px";
  const radiusOuter = size >= 28 ? 7 : 6;
  const radiusInner = size >= 28 ? 2 : 1;
  const cell: CSSProperties = { borderRadius: radiusInner };
  return (
    <span style={{ width: size, height: size, borderRadius: radiusOuter, background: "var(--accent)", display: "grid", gridTemplateColumns: "1fr 1fr", gap, padding: pad, flexShrink: 0 }}>
      <span style={{ ...cell, background: "var(--accent-fg)" }} />
      <span style={{ ...cell, border: `${border} solid var(--accent-fg)` }} />
      <span style={{ ...cell, border: `${border} solid var(--accent-fg)` }} />
      <span style={{ ...cell, background: "var(--accent-fg)" }} />
    </span>
  );
}
