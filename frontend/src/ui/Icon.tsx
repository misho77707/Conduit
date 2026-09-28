import type { CSSProperties } from "react";

export function Icon({ name, style, className, spin }: { name: string; style?: CSSProperties; className?: string; spin?: boolean }) {
  return (
    <i className={`sym${className ? ` ${className}` : ""}`} style={{ ...style, animation: spin ? "spin 1.2s linear infinite" : style?.animation }}>
      {name}
    </i>
  );
}
