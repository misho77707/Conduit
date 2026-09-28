import type { ReactNode } from "react";
import { IconButton } from "./Button";

export function Modal({
  width = 520, title, sub, onClose, children, footer,
}: { width?: number; title: string; sub?: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  return (
    <div style={{ position: "fixed", inset: 0, background: "var(--overlay)", display: "flex", alignItems: "center", justifyContent: "center", padding: 24, zIndex: 50 }}>
      <div style={{ width, maxWidth: "100%", maxHeight: "calc(100vh - 48px)", display: "flex", flexDirection: "column", background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, boxShadow: "var(--shadow-3)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "16px 20px", borderBottom: "1px solid var(--border)" }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
            {sub && <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 2 }}>{sub}</div>}
          </div>
          <IconButton icon="close" onClick={onClose} size={30} />
        </div>
        <div style={{ padding: 20, overflow: "auto", display: "flex", flexDirection: "column", gap: 16 }}>{children}</div>
        {footer && <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "14px 20px", borderTop: "1px solid var(--border)" }}>{footer}</div>}
      </div>
    </div>
  );
}
