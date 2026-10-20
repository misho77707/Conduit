import type { ButtonHTMLAttributes, CSSProperties, MouseEvent } from "react";
import { Icon } from "./Icon";

type Variant = "primary" | "outline" | "ghost" | "danger" | "danger-solid";

const BASE: CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 6, borderRadius: 6, fontWeight: 500,
  cursor: "pointer", whiteSpace: "nowrap", border: "1px solid transparent",
};

const VARIANT: Record<Variant, CSSProperties> = {
  primary: { background: "var(--accent)", color: "var(--accent-fg)" },
  outline: { background: "var(--surface)", borderColor: "var(--border-2)", color: "var(--text)" },
  ghost: { background: "transparent", color: "var(--text-2)" },
  danger: { background: "var(--surface)", borderColor: "var(--border-2)", color: "var(--err)" },
  "danger-solid": { background: "var(--err)", color: "#fff" },
};

const HOVER_CLASS: Record<Variant, string> = {
  primary: "hover-bright", outline: "hover-surface2", ghost: "hover-surface2", danger: "hover-err-soft", "danger-solid": "hover-bright",
};

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  icon?: string;
  height?: number;
  padX?: number;
}

export function Button({ variant = "outline", icon, height = 32, padX = 12, style, className, children, disabled, ...rest }: Props) {
  return (
    <button
      className={`${HOVER_CLASS[variant]}${className ? ` ${className}` : ""}`}
      disabled={disabled}
      style={{ ...BASE, ...VARIANT[variant], height, padding: `0 ${padX}px`, opacity: disabled ? 0.45 : 1, ...style }}
      {...rest}
    >
      {icon && <Icon name={icon} style={{ fontSize: 17 }} />}
      {children}
    </button>
  );
}

export function IconButton({
  icon, title, onClick, size = 28, active, style, disabled,
}: { icon: string; title?: string; onClick?: (e: MouseEvent<HTMLButtonElement>) => void; size?: number; active?: boolean; style?: CSSProperties; disabled?: boolean }) {
  return (
    <button
      title={title}
      onClick={onClick}
      disabled={disabled}
      className="hover-surface2"
      style={{
        width: size, height: size, borderRadius: 6, border: "none",
        background: active ? "var(--surface-3)" : "transparent", color: "var(--text-2)",
        display: "flex", alignItems: "center", justifyContent: "center", cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.35 : 1, ...style,
      }}
    >
      <Icon name={icon} />
    </button>
  );
}
