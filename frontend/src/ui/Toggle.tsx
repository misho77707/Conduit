export function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        width: 28, height: 16, borderRadius: 8, border: "none", padding: 2,
        background: on ? "var(--accent)" : "var(--border-2)", cursor: "pointer", display: "flex", flexShrink: 0,
        transition: "background .15s",
      }}
    >
      <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#fff", transform: `translateX(${on ? "12px" : "0px"})`, transition: "transform .15s" }} />
    </button>
  );
}

export function ToggleRow({ on, onClick, title, sub }: { on: boolean; onClick: () => void; title: string; sub?: string }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
      <div style={{ marginTop: 2 }}>
        <Toggle on={on} onClick={onClick} />
      </div>
      <div>
        <div style={{ fontWeight: 500 }}>{title}</div>
        {sub && <div style={{ fontSize: 12, color: "var(--text-3)" }}>{sub}</div>}
      </div>
    </div>
  );
}
