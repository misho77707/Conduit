import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { Icon } from "../ui/Icon";

export type ToastKind = "ok" | "warn" | "err" | "info";
interface Toast { id: number; kind: ToastKind; title: string; msg?: string; }

const TONE: Record<ToastKind, [string, string]> = {
  ok: ["check_circle", "var(--ok)"],
  err: ["error", "var(--err)"],
  info: ["info", "var(--accent)"],
  warn: ["warning", "var(--warn)"],
};

const ToastContext = createContext<((kind: ToastKind, title: string, msg?: string) => void) | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((kind: ToastKind, title: string, msg?: string) => {
    const id = Math.random();
    setToasts((s) => [...s, { id, kind, title, msg }]);
    setTimeout(() => setToasts((s) => s.filter((t) => t.id !== id)), 4200);
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div style={{ position: "fixed", right: 20, bottom: 20, display: "flex", flexDirection: "column", gap: 8, zIndex: 100, pointerEvents: "none" }}>
        {toasts.map((t) => {
          const [icon, c] = TONE[t.kind];
          return (
            <div
              key={t.id}
              style={{
                width: 360, maxWidth: "calc(100vw - 40px)", display: "flex", gap: 10, padding: "12px 14px",
                background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8,
                boxShadow: "var(--shadow-2)", animation: "toastin .2s ease-out", pointerEvents: "auto",
              }}
            >
              <Icon name={icon} style={{ color: c, fontSize: 19 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 500 }}>{t.title}</div>
                {t.msg && <div style={{ color: "var(--text-2)", fontSize: 12, marginTop: 2 }}>{t.msg}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx;
}
