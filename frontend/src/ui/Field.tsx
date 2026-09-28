import type { ReactNode } from "react";

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-2)" }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: "var(--text-3)" }}>{hint}</span>}
    </label>
  );
}
