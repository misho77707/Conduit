import { Icon } from "../ui/Icon";

export function ComingSoon({ title, icon, note }: { title: string; icon: string; note: string }) {
  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1440 }}>
      <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>{title}</h1>
      <div style={{ background: "var(--surface)", border: "1px dashed var(--border-2)", borderRadius: 10, padding: "64px 24px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", gap: 14 }}>
        <span style={{ width: 48, height: 48, borderRadius: 12, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Icon name={icon} style={{ fontSize: 26 }} />
        </span>
        <div>
          <div style={{ fontSize: 16, fontWeight: 600 }}>Coming soon</div>
          <p style={{ margin: "6px auto 0", maxWidth: 420, color: "var(--text-2)" }}>{note}</p>
        </div>
      </div>
    </div>
  );
}
