export function ProgressBar({ pct, color = "var(--accent)", striped, height = 10 }: { pct: number; color?: string; striped?: boolean; height?: number }) {
  return (
    <div style={{ height, borderRadius: height / 2, background: "var(--surface-3)", overflow: "hidden" }}>
      <div
        style={{
          height: "100%", width: `${Math.min(100, Math.max(0, pct))}%`, backgroundColor: color,
          backgroundImage: striped
            ? "linear-gradient(45deg,rgba(255,255,255,.18) 25%,transparent 25%,transparent 50%,rgba(255,255,255,.18) 50%,rgba(255,255,255,.18) 75%,transparent 75%)"
            : undefined,
          backgroundSize: striped ? "28px 28px" : undefined,
          animation: striped ? "stripes 1s linear infinite" : undefined,
          transition: "width .45s linear",
        }}
      />
    </div>
  );
}
