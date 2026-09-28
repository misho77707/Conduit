import type { CSSProperties, ReactNode } from "react";
import { Icon } from "./Icon";

export function Card({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, minWidth: 0, ...style }}>
      {children}
    </div>
  );
}

export function CardHeader({ icon, title, right }: { icon?: string; title: string; right?: ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
      {icon && <Icon name={icon} style={{ color: "var(--text-3)" }} />}
      <span style={{ fontWeight: 600, flex: 1 }}>{title}</span>
      {right}
    </div>
  );
}

export function CardBody({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 14, ...style }}>{children}</div>;
}
