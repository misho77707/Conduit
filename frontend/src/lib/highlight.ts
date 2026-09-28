// Tiny SQL/Python token colorer for the query editor and generated-DDL panels.
// Ported from the design prototype's `C.hl` (frontend/design/Conduit v2.dc.html).

export interface Seg { t: string; c: string; }
export interface HlLine { n: number; segs: Seg[]; }

const KEYWORDS = new Set(
  ("SELECT FROM LEFT JOIN ON AND AS CREATE TABLE IF NOT EXISTS ENGINE ORDER BY PARTITION PRIMARY KEY " +
    "TRUNCATE INSERT INTO VALUES WHERE NULL COALESCE ROUND CAST DROP UPDATE CONFLICT DO SET DUPLICATE " +
    "import def return from if else for in not and or None True False").split(" "),
);

const TYPE_RE = /^(u?int\d*|decimal|numeric|string|datetime64|datetime|bool|boolean|date|float64|bigint|integer|int|varchar|text|timestamptz|timestamp|double|precision|tinyint|real|char|fixedstring|lowcardinality|mergetree|replacingmergetree|toYYYYMM|ISODate|Decimal128)$/i;

export function highlight(src: string, lang: "sql" | "py" = "sql"): HlLine[] {
  const re =
    lang === "py"
      ? /(#.*$|"[^"]*"|'[^']*'|\b\d+(?:\.\d+)?\b|[A-Za-z_]\w*|\s+|[^\sA-Za-z_\d"'#]+|.)/g
      : /(--.*$|"[^"]*"|'[^']*'|\b\d+(?:\.\d+)?\b|[A-Za-z_]\w*|\s+|[^\sA-Za-z_\d"'-]+|.)/g;
  return src.split("\n").map((line, i) => {
    const segs: Seg[] = [];
    let m: RegExpExecArray | null;
    re.lastIndex = 0;
    while ((m = re.exec(line))) {
      const t = m[0];
      let c = "var(--text)";
      if (/^(--|#)/.test(t)) c = "var(--text-3)";
      else if (/^["']/.test(t)) c = "var(--code-str)";
      else if (/^\d/.test(t)) c = "var(--code-num)";
      else if (KEYWORDS.has(t)) c = "var(--code-kw)";
      else if (TYPE_RE.test(t)) c = "var(--code-type)";
      segs.push({ t, c });
      if (t === "") break; // guard against zero-width matches looping forever
    }
    return { n: i + 1, segs };
  });
}
