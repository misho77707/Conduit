"""CREATE TABLE generation per dialect, from logical column specs.

The same text is shown in the wizard's DDL preview and executed when the target table is created,
so what the user reviews is what runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import ColSpec, clean, to_dialect

_TIME = {"DATE", "TIMESTAMP", "TIMESTAMPTZ"}
_INT = {"SMALLINT", "INTEGER", "BIGINT"}


def qi(dialect: str, name: str) -> str:
    """Quote one identifier."""
    if dialect in ("mysql", "clickhouse"):
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def split_name(table: str) -> tuple[str | None, str]:
    schema, sep, name = table.partition(".")
    return (schema, name) if sep else (None, table)


def qualified(dialect: str, table: str) -> str:
    schema, name = split_name(table)
    if schema is None:
        return qi(dialect, name)
    return f"{qi(dialect, schema)}.{qi(dialect, name)}"


@dataclass
class TargetOptions:
    order_by: list[str] = field(default_factory=list)  # ClickHouse ORDER BY (defaults from keys / time+id columns)
    partition_by: str | None = None  # ClickHouse partition expression, e.g. toYYYYMM(posted_at)
    est_rows: int | None = None
    ddl: list[str] | None = None  # user-edited statements that replace the generated CREATE ones (new tables only)


def split_statements(sql: str) -> list[str]:
    """Split SQL text on semicolons that are outside quotes and comments; empty statements are dropped."""
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            buf.append(ch)
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = len(sql) if end < 0 else end
            buf.append(sql[i:end])
            i = end
            continue
        elif ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip() and not all(ln.strip().startswith("--") or not ln.strip()
                                                            for ln in s.splitlines())]


def default_clickhouse_order(cols: list[ColSpec], keys: list[str]) -> list[str]:
    if keys:
        return keys
    time_col = next((c.name for c in cols if clean(c.logical) in _TIME), None)
    id_col = next((c.name for c in cols if clean(c.logical) in _INT), None)
    picked = [c for c in (time_col, id_col) if c]
    return picked


def create_table_sql(dialect: str, table: str, cols: list[ColSpec], keys: list[str], mode: str,
                     opts: TargetOptions | None = None) -> str:
    opts = opts or TargetOptions()
    keyset = set(keys)
    fq = qualified(dialect, table)
    order = (opts.order_by or default_clickhouse_order(cols, keys)) if dialect == "clickhouse" else []
    lines = []
    for c in cols:
        t = to_dialect(dialect, c.logical, key=c.name in keyset, nullable=c.nullable and c.name not in keyset)
        null = "" if dialect == "clickhouse" else (" NOT NULL" if (c.name in keyset) else "")
        lines.append(f"    {qi(dialect, c.name)} {t}{null}")

    if dialect == "clickhouse":
        engine = "ReplacingMergeTree" if mode == "upsert" else "MergeTree"
        body = ",\n".join(lines)
        order_sql = "tuple()" if not order else "(" + ", ".join(qi(dialect, o) for o in order) + ")"
        sql = f"CREATE TABLE IF NOT EXISTS {fq} (\n{body}\n)\nENGINE = {engine}"
        if opts.partition_by:
            sql += f"\nPARTITION BY {opts.partition_by}"
        sql += f"\nORDER BY {order_sql}"
        # ClickHouse refuses NULLable sort / partition columns unless told otherwise; NULLs are kept, not defaulted
        in_key = set(order) | {c.name for c in cols if opts.partition_by and c.name in opts.partition_by}
        if any(c.nullable and c.name in in_key and c.name not in keyset for c in cols):
            sql += "\nSETTINGS allow_nullable_key = 1"
        return sql

    if keys and mode == "upsert":
        lines.append(f"    PRIMARY KEY ({', '.join(qi(dialect, k) for k in keys)})")
    body = ",\n".join(lines)
    tail = " DEFAULT CHARSET=utf8mb4" if dialect == "mysql" else ""
    return f"CREATE TABLE IF NOT EXISTS {fq} (\n{body}\n){tail}"


def suggest_partition(cols: list[ColSpec], est_rows: int | None) -> str | None:
    """Monthly partitions on the first date/timestamp column, for big tables only."""
    if not est_rows or est_rows < 5_000_000:
        return None
    col = next((c.name for c in cols if clean(c.logical) in _TIME), None)
    return f"toYYYYMM({qi('clickhouse', col)})" if col else None
