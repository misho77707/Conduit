export function SegmentedControl<T extends string>({ options, value, onChange, small }: {
  options: { id: T; label: string }[]; value: T; onChange: (id: T) => void; small?: boolean;
}) {
  return (
    <div style={{ display: "inline-flex", padding: 2, borderRadius: 7, background: "var(--surface-3)" }}>
      {options.map((o) => {
        const active = o.id === value;
        return (
          <button
            key={o.id}
            onClick={() => onChange(o.id)}
            style={{
              height: small ? 24 : 28, padding: small ? "0 10px" : "0 14px", borderRadius: 5, border: "none",
              background: active ? "var(--surface)" : "transparent", color: active ? "var(--text)" : "var(--text-2)",
              boxShadow: active ? "var(--shadow-1)" : "none", fontWeight: 500, fontSize: small ? 12 : undefined, cursor: "pointer",
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
