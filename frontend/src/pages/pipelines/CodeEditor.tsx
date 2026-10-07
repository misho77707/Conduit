import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { highlight } from "../../lib/highlight";
import { complete, insertion, type Completion, type Kind } from "./pyComplete";

const LINE = 20, PAD_TOP = 10, GUTTER = 40, INDENT = "    ";
const KIND_STYLE: Record<Kind, { tag: string; color: string }> = {
  method: { tag: "m", color: "var(--code-kw)" },
  function: { tag: "f", color: "var(--code-str)" },
  property: { tag: "p", color: "var(--code-type)" },
  column: { tag: "c", color: "var(--accent)" },
  variable: { tag: "v", color: "var(--code-num)" },
  module: { tag: "M", color: "var(--code-num)" },
  constant: { tag: "k", color: "var(--code-type)" },
  keyword: { tag: "k", color: "var(--text-3)" },
};

/**
 * Highlighted code with a transparent textarea on top. For Python (`suggest`), a completion popup offers
 * pandas / numpy members and the step's input column names (see pyComplete.ts).
 */
export function CodeEditor({ code, onChange, lang, suggest = false, columns = [] }: {
  code: string; onChange: (v: string) => void; lang: "sql" | "py"; suggest?: boolean; columns?: string[];
}) {
  const ta = useRef<HTMLTextAreaElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const meter = useRef<HTMLSpanElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const pendingCaret = useRef<number | null>(null);
  const [charW, setCharW] = useState(7.2);
  const [comp, setComp] = useState<(Completion & { caret: number }) | null>(null);
  const [active, setActive] = useState(0);

  useEffect(() => {
    const measure = () => meter.current && setCharW(meter.current.getBoundingClientRect().width / 20);
    measure();
    document.fonts?.ready.then(measure).catch(() => {});
  }, []);

  // apply a caret position chosen while editing, once React has rendered the new text
  useLayoutEffect(() => {
    if (pendingCaret.current != null && ta.current) {
      ta.current.setSelectionRange(pendingCaret.current, pendingCaret.current);
      pendingCaret.current = null;
    }
  }, [code]);

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const lines = highlight(code, lang);

  function edit(next: string, caret: number) {
    pendingCaret.current = caret;
    onChange(next);
  }

  function refresh(text: string, caret: number, force = false) {
    if (!suggest) return;
    const c = complete(text, caret, columns, force);
    setComp(c ? { ...c, caret } : null);
    setActive(0);
  }

  function accept(i: number) {
    if (!comp) return;
    const item = comp.items[i];
    const { text, back } = insertion(item, comp);
    const next = code.slice(0, comp.from) + text + code.slice(comp.to);
    setComp(null);
    edit(next, comp.from + text.length - back);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    const el = e.currentTarget, start = el.selectionStart, end = el.selectionEnd;
    if (comp) {
      if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => (a + 1) % comp.items.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => (a - 1 + comp.items.length) % comp.items.length); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); accept(active); return; }
      if (e.key === "Escape") { e.preventDefault(); setComp(null); return; }
    }
    if (suggest && e.key === " " && e.ctrlKey) { e.preventDefault(); refresh(code, start, true); return; }
    if (e.key === "Tab" && !e.shiftKey && start === end) {
      e.preventDefault();
      edit(code.slice(0, start) + INDENT + code.slice(end), start + INDENT.length);
      return;
    }
    if (e.key === "Enter" && lang === "py" && start === end) {
      e.preventDefault();
      const line = code.slice(code.lastIndexOf("\n", start - 1) + 1, start);
      const indent = (/^\s*/.exec(line)?.[0] || "") + (/:\s*(#.*)?$/.test(line) ? INDENT : "");
      edit(code.slice(0, start) + "\n" + indent + code.slice(end), start + 1 + indent.length);
    }
  }

  function onInput(next: string, caret: number) {
    onChange(next);
    if (!suggest) return;
    const typed = next.length > code.length ? next.slice(caret - (next.length - code.length), caret) : "";
    // open on ".", quotes / "[" (column names) and identifiers; keep an open popup in sync while deleting
    if ((typed.length === 1 && /[\w."'[]/.test(typed)) || (comp && next.length < code.length)) refresh(next, caret);
    else setComp(null);
  }

  // popup position: just below the caret
  let pos = { left: 0, top: 0 };
  if (comp && ta.current) {
    const before = code.slice(0, comp.caret);
    const row = before.split("\n").length - 1;
    const col = comp.caret - (before.lastIndexOf("\n") + 1);
    const width = box.current?.clientWidth || 600;
    pos = {
      top: PAD_TOP + (row + 1) * LINE - ta.current.scrollTop + 2,
      left: Math.max(0, Math.min(GUTTER + col * charW - ta.current.scrollLeft - (comp.caret - comp.from) * charW, width - 470)),
    };
  }
  const cur = comp?.items[active];

  return (
    <div ref={box} style={{ position: "relative", border: "1px solid var(--border)", borderRadius: 6, background: "var(--code-bg)", minHeight: 160 }}>
      <span ref={meter} aria-hidden style={{ position: "absolute", visibility: "hidden", whiteSpace: "pre", fontFamily: "'Geist Mono',monospace", fontSize: 12 }}>{"M".repeat(20)}</span>
      <div style={{ padding: `${PAD_TOP}px 0`, fontFamily: "'Geist Mono',monospace", fontSize: 12, lineHeight: `${LINE}px` }}>
        {lines.map((ln) => (
          <div key={ln.n} style={{ display: "flex", whiteSpace: "pre", minHeight: LINE }}>
            <span style={{ width: GUTTER, flexShrink: 0, textAlign: "right", paddingRight: 12, color: "var(--text-3)", userSelect: "none" }}>{ln.n}</span>
            <span>{ln.segs.map((sg, i) => <span key={i} style={{ color: sg.c }}>{sg.t}</span>)}</span>
          </div>
        ))}
      </div>
      <textarea
        ref={ta}
        value={code} spellCheck={false}
        onChange={(e) => onInput(e.target.value, e.target.selectionStart)}
        onKeyDown={onKeyDown}
        onClick={() => setComp(null)}
        onBlur={() => setComp(null)}
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", minHeight: 160, padding: `${PAD_TOP}px 0 ${PAD_TOP}px ${GUTTER}px`, margin: 0, border: "none", outline: "none", resize: "none", background: "transparent", color: "transparent", caretColor: "var(--text)", fontFamily: "'Geist Mono',monospace", fontSize: 12, lineHeight: `${LINE}px`, whiteSpace: "pre", overflow: "auto" }}
      />
      {comp && cur && (
        <div
          onMouseDown={(e) => e.preventDefault() /* keep focus in the textarea */}
          style={{ position: "absolute", left: pos.left, top: pos.top, zIndex: 20, display: "flex", width: 470, maxWidth: "calc(100vw - 32px)", background: "var(--surface)", border: "1px solid var(--border-2)", borderRadius: 7, boxShadow: "var(--shadow-2)", overflow: "hidden", fontSize: 12 }}
        >
          <div ref={listRef} style={{ width: 210, flexShrink: 0, maxHeight: 220, overflowY: "auto", borderRight: "1px solid var(--border)", padding: 3 }}>
            {comp.items.map((it, i) => (
              <div key={it.kind + it.label} data-i={i} onClick={() => accept(i)} onMouseEnter={() => setActive(i)}
                style={{ display: "flex", alignItems: "center", gap: 7, height: 24, padding: "0 6px", borderRadius: 4, cursor: "pointer", background: i === active ? "var(--accent-soft)" : "transparent" }}>
                <span style={{ width: 15, height: 15, borderRadius: 3, flexShrink: 0, border: `1px solid ${KIND_STYLE[it.kind].color}`, color: KIND_STYLE[it.kind].color, fontSize: 9, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Geist Mono',monospace" }}>{KIND_STYLE[it.kind].tag}</span>
                <span style={{ fontFamily: "'Geist Mono',monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  <b style={{ fontWeight: 600, color: "var(--accent)" }}>{it.label.slice(0, comp.prefix.length).toLowerCase() === comp.prefix.toLowerCase() ? it.label.slice(0, comp.prefix.length) : ""}</b>
                  {it.label.slice(0, comp.prefix.length).toLowerCase() === comp.prefix.toLowerCase() ? it.label.slice(comp.prefix.length) : it.label}
                </span>
              </div>
            ))}
          </div>
          <div style={{ flex: 1, minWidth: 0, padding: "8px 10px", display: "flex", flexDirection: "column", gap: 6 }}>
            <span style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.05em", color: KIND_STYLE[cur.kind].color, fontWeight: 600 }}>{cur.kind}</span>
            {cur.sig && <code style={{ fontFamily: "'Geist Mono',monospace", fontSize: 11, color: "var(--text)", overflowWrap: "anywhere" }}>{cur.sig}</code>}
            {cur.doc && <span style={{ color: "var(--text-2)", lineHeight: 1.45 }}>{cur.doc}</span>}
            <span style={{ marginTop: "auto", fontSize: 10, color: "var(--text-3)" }}>↑↓ choose · Enter/Tab insert · Esc close</span>
          </div>
        </div>
      )}
    </div>
  );
}
