import { forwardRef, type InputHTMLAttributes, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";

const BASE = {
  height: 32, padding: "0 10px", borderRadius: 6, border: "1px solid var(--border-2)",
  background: "var(--surface)", outline: "none", width: "100%",
} as const;

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { mono?: boolean }>(
  ({ mono = true, style, className, ...rest }, ref) => (
    <input
      ref={ref}
      className={`focus-ring${className ? ` ${className}` : ""}`}
      style={{ ...BASE, fontFamily: mono ? "'Geist Mono',monospace" : undefined, ...style }}
      {...rest}
    />
  ),
);
Input.displayName = "Input";

export function Select({ style, className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={`focus-ring${className ? ` ${className}` : ""}`} style={{ ...BASE, height: 32, fontFamily: "'Geist Mono',monospace", fontSize: 12, ...style }} {...rest}>
      {children}
    </select>
  );
}

export function TextArea({ style, className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={`focus-ring${className ? ` ${className}` : ""}`}
      style={{ padding: "6px 10px", borderRadius: 6, border: "1px solid var(--border-2)", background: "var(--surface)", outline: "none", fontFamily: "'Geist Mono',monospace", fontSize: 12, resize: "vertical", width: "100%", ...style }}
      {...rest}
    />
  );
}
