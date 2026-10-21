import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { IconButton } from "./Button";
import { Icon } from "./Icon";

export interface RowMenuItem {
  icon: string;
  label: string;
  onClick: () => void;
  tone?: string; // text colour, e.g. "var(--err)" for Delete
  disabled?: boolean;
  hint?: string; // tooltip; for a disabled item, why it is
}

const ITEM_H = 30, PAD = 4;

/**
 * "⋯" button with a small action menu. The menu is portalled to <body> and positioned against the viewport,
 * so a table's scroll container (overflow: auto) can never clip it; near the bottom of the window it opens
 * upward. It closes on an outside click, Escape, scroll or resize (a fixed menu would drift from its row).
 */
export function RowMenu({ items, width = 160, size = 28, title = "More" }: { items: RowMenuItem[]; width?: number; size?: number; title?: string }) {
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    if (!pos) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [pos]);

  function toggle(anchor: HTMLElement) {
    if (pos) return setPos(null);
    const r = anchor.getBoundingClientRect();
    const h = items.length * ITEM_H + PAD * 2 + 2;
    const below = r.bottom + 4 + h <= window.innerHeight;
    setPos({
      left: Math.min(Math.max(8, r.right - width), window.innerWidth - width - 8),
      top: below ? r.bottom + 4 : Math.max(8, r.top - 4 - h),
    });
  }

  return (
    <>
      <IconButton icon="more_horiz" title={title} size={size} active={!!pos} onClick={(e) => toggle(e.currentTarget)} />
      {pos && createPortal(
        <>
          <div onMouseDown={() => setPos(null)} style={{ position: "fixed", inset: 0, zIndex: 49 }} />
          <div role="menu" style={{ position: "fixed", left: pos.left, top: pos.top, width, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, boxShadow: "var(--shadow-2)", padding: PAD, zIndex: 50, display: "flex", flexDirection: "column" }}>
            {items.map((it) => (
              <button
                key={it.label} role="menuitem" aria-disabled={it.disabled || undefined} title={it.hint}
                onClick={() => { if (it.disabled) return; setPos(null); it.onClick(); }}
                className="hover-surface2"
                style={{ height: ITEM_H, width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "0 8px", border: "none", background: "transparent", borderRadius: 5, cursor: it.disabled ? "default" : "pointer", textAlign: "left", color: it.disabled ? "var(--text-3)" : it.tone || "var(--text-2)", whiteSpace: "nowrap" }}
              >
                <Icon name={it.icon} style={{ fontSize: 16 }} />{it.label}
              </button>
            ))}
          </div>
        </>,
        document.body,
      )}
    </>
  );
}
