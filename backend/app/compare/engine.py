"""Compare a source with a target: read both sides into a scratch DuckDB file, diff them in SQL, keep the results.

Both sides are read through the same adapters the load engine uses (any saved connection, a table or a query; the
source may also be a file). Column i of a pair is stored as c<i> on both sides, so the SQL never has to quote user
names. Results go to <data>/compare/<id>/results.duckdb, named after the target columns; the scratch copy of the
data is deleted as soon as the diff is done.

Rows are matched on key columns (default: the target's primary key) which gives three sets - missing in the
target, extra in the target, and same key with different values. Without a key, whole rows are compared as
multisets (EXCEPT ALL), so a changed row shows up as one missing plus one extra row.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import select

from ..config import get_settings
from ..runtime import current_settings
from ..db import get_sessionmaker
from ..engine import staging
from ..engine.staging import StageInput
from ..engine.runner import JobError, short_error
from ..engine.sources import raw_columns, source_adapter, source_connection
from ..engine.spec import SourceIn
from ..engine.types import ColSpec
from ..models import Comparison, utcnow
from ..services import build_connector
from .spec import ColumnPair, CompareIn

log = logging.getLogger("conduit.compare")

SETS = ("missing", "extra", "mismatched", "dup_source", "dup_target")
FINISHED = ("succeeded", "failed", "cancelled")


class Cancelled(Exception):
    pass


@dataclass
class Pair:
    i: int  # stored as c<i> on both sides
    source: ColSpec
    target: ColSpec

    @property
    def name(self) -> str:  # results are named after the target column
        return self.target.name


@dataclass
class Resolved:
    src: Any
    tgt: Any
    src_cols: list[ColSpec]
    tgt_cols: list[ColSpec]
    pairs: list[Pair]
    keys: list[str]
    key_note: str
    unpaired_source: list[str]
    unpaired_target: list[str]
    notes: list[str] = field(default_factory=list)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# ---- columns, pairs and keys -----------------------------------------------------------

def pair_columns(src_cols: list[ColSpec], tgt_cols: list[ColSpec], wanted: list[ColumnPair] | None,
                 *, strict: bool = True) -> tuple[list[Pair], list[str], list[str]]:
    """(pairs, unpaired source names, unpaired target names). Without `wanted`, same names pair up (any case)."""
    s_by, t_by = {c.name: c for c in src_cols}, {c.name: c for c in tgt_cols}
    found: list[tuple[ColSpec, ColSpec]] = []
    if wanted is None:
        t_lower: dict[str, ColSpec] = {}
        for c in tgt_cols:
            t_lower.setdefault(c.name.lower(), c)
        used: set[str] = set()
        for c in src_cols:
            t = t_lower.get(c.name.lower())
            if t is not None and t.name not in used:
                found.append((c, t))
                used.add(t.name)
    else:
        used = set()
        for w in wanted:
            if not w.include:
                continue
            if w.source not in s_by:
                raise JobError(f"Column {w.source!r} is not in the source")
            if w.target not in t_by:
                raise JobError(f"Column {w.target!r} is not in the target")
            if w.target in used:
                raise JobError(f"Target column {w.target!r} is paired twice")
            used.add(w.target)
            found.append((s_by[w.source], t_by[w.target]))
    if strict and not found:
        raise JobError("No columns to compare: no column names match between the two sides, so pair them by hand")
    pairs = [Pair(i, s, t) for i, (s, t) in enumerate(found)]
    paired_s, paired_t = {p.source.name for p in pairs}, {p.target.name for p in pairs}
    return pairs, [c.name for c in src_cols if c.name not in paired_s], [c.name for c in tgt_cols if c.name not in paired_t]


def suggest_keys(session, source: SourceIn, target: SourceIn, pairs: list[Pair]) -> tuple[list[str], str]:
    """The target table's primary key (else the source's), as target column names, when all of it is compared."""
    for role, side, on_target in (("target", target, True), ("source", source, False)):
        if not (side.table and side.connection_id and not side.is_file):
            continue
        try:
            schema = build_connector(source_connection(session, side, role)).inspect(side.table)
        except Exception:  # noqa: BLE001 - no key suggestion is not an error
            continue
        pk = [c.name for c in schema.columns if c.primary_key]
        by = {(p.target.name if on_target else p.source.name): p.target.name for p in pairs}
        if pk and all(k in by for k in pk):
            return [by[k] for k in pk], f"primary key of the {role} table"
    return [], "no primary key found, so whole rows are compared"


def describe(session, source: SourceIn, target: SourceIn) -> dict[str, Any]:
    """What the setup screen needs: both column lists, the automatic pairing and the suggested key."""
    src = source_adapter(session, source, "source")
    s_cols = raw_columns(src, source, "source")
    tgt = source_adapter(session, target, "target")
    t_cols = raw_columns(tgt, target, "target")
    pairs, us, ut = pair_columns(s_cols, t_cols, None, strict=False)
    keys, note = suggest_keys(session, source, target, pairs)
    return {
        "source_columns": [{"name": c.name, "type": c.logical} for c in s_cols],
        "target_columns": [{"name": c.name, "type": c.logical} for c in t_cols],
        "pairs": [{"source": p.source.name, "target": p.target.name} for p in pairs],
        "unpaired_source": us, "unpaired_target": ut, "suggested_keys": keys, "key_note": note,
    }


def resolve(session, spec: CompareIn, work: Path | None = None) -> Resolved:
    """`work`: the comparison is running, so a remote side is downloaded in full under work/remote/ (only
    previewed otherwise, e.g. when validating the request)."""
    full = (lambda role: work / "remote" / role) if work is not None else (lambda role: None)  # noqa: E731
    src = source_adapter(session, spec.source, "source", full_dir=full("source"))
    s_cols = raw_columns(src, spec.source, "source")
    tgt = source_adapter(session, spec.target, "target", full_dir=full("target"))
    t_cols = raw_columns(tgt, spec.target, "target")
    pairs, us, ut = pair_columns(s_cols, t_cols, spec.columns)
    names = [p.name for p in pairs]
    if spec.keys is None:
        keys, note = suggest_keys(session, spec.source, spec.target, pairs)
    else:
        missing = [k for k in spec.keys if k not in names]
        if missing:
            raise JobError(f"Key column(s) {', '.join(missing)} are not among the compared columns")
        keys = list(spec.keys)
        note = "chosen" if keys else "no key chosen, so whole rows are compared"
    notes = []
    if spec.options.ignore_nulls and not keys:
        notes.append("'Ignore nulls' only applies when rows are matched by key; whole rows were compared exactly.")
    return Resolved(src, tgt, s_cols, t_cols, pairs, keys, note, us, ut, notes)


# ---- reading ---------------------------------------------------------------------------




# ---- normalising the two sides to comparable types ---------------------------------------

_INTS = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}


def _cls(t: str) -> str:
    t = t.upper()
    if t.startswith("VARCHAR"):
        return "text"
    if t in _INTS:
        return "int"
    if t.startswith("DECIMAL"):
        return "dec"
    if t in ("FLOAT", "DOUBLE", "REAL"):
        return "float"
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP"):
        return "ts"
    if t == "BOOLEAN":
        return "bool"
    if t in ("NULL", '"NULL"'):
        return "null"
    return "other"


def _scale(t: str) -> int:
    m = re.match(r"DECIMAL\(\d+,\s*(\d+)\)", t.upper())
    return int(m.group(1)) if m else 0


def common_type(a: str, b: str) -> tuple[str, str]:
    """(DuckDB type both sides are cast to, kind) for one column pair, from their stored DuckDB types."""
    ca, cb = _cls(a), _cls(b)
    if ca == "null":
        ca, a = cb, b
    if cb == "null":
        cb, b = ca, a
    if ca == "text" and cb not in ("text", "other", "null"):  # typed vs text: read the text as that type
        ca, a = cb, b
    if cb == "text" and ca not in ("text", "other", "null"):
        cb, b = ca, a
    if ca == cb == "text" or "other" in (ca, cb) or "null" in (ca, cb):
        return "VARCHAR", "text"
    num = {"int", "dec", "float"}
    if ca in num and cb in num:
        if "float" in (ca, cb):
            return "DOUBLE", "float"
        if ca == cb == "int" and not {a.upper(), b.upper()} & {"HUGEINT", "UBIGINT", "UHUGEINT"}:
            return "BIGINT", "exact"
        return f"DECIMAL(38,{max(_scale(a), _scale(b))})", "exact"
    if {ca, cb} <= {"date", "ts"}:
        return ("DATE", "date") if ca == cb == "date" else ("TIMESTAMP", "ts")
    if ca == cb == "bool":
        return "BOOLEAN", "bool"
    return "VARCHAR", "text"


def _side_expr(col: str, stored: str, common: str, kind: str, opts) -> str:
    e = col
    if kind == "text":
        e = f"CAST({e} AS VARCHAR)"
        if opts.trim_text:
            e = f"trim({e})"
        if opts.case_insensitive:
            e = f"lower({e})"
        return e
    if _cls(stored) == "text":
        e = f"TRY_CAST(trim({e}) AS {common})"  # text that does not parse becomes NULL (and so differs)
    elif stored.upper() != common.upper():
        e = f"CAST({e} AS {common})"
    if kind == "float":
        e = f"round({e}, {int(opts.float_decimals)})"
    return e


# ---- the diff --------------------------------------------------------------------------

def _count(con, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0] or 0)


def _diff(con: duckdb.DuckDBPyConnection, r: Resolved, spec: CompareIn) -> dict[str, Any]:
    o = spec.options
    st = dict(con.execute("SELECT column_name, column_type FROM (DESCRIBE src)").fetchall())
    tt = dict(con.execute("SELECT column_name, column_type FROM (DESCRIBE tgt)").fetchall())
    cols_meta, s_sel, t_sel = [], [], []
    for p in r.pairs:
        c = f"c{p.i}"
        common, kind = common_type(st[c], tt[c])
        s_sel.append(f"{_side_expr(c, st[c], common, kind, o)} AS {c}")
        t_sel.append(f"{_side_expr(c, tt[c], common, kind, o)} AS {c}")
        cols_meta.append({"name": p.name, "source": p.source.name, "source_type": p.source.logical,
                          "target_type": p.target.logical, "compared_as": common})
    con.execute(f"CREATE TEMP VIEW s AS SELECT {', '.join(s_sel)} FROM src")
    con.execute(f"CREATE TEMP VIEW t AS SELECT {', '.join(t_sel)} FROM tgt")

    by_name = {p.name: p for p in r.pairs}
    out = lambda alias: ", ".join(f"{alias}.c{p.i} AS {_q(p.name)}" for p in r.pairs)  # noqa: E731
    rows_s, rows_t = _count(con, "SELECT count(*) FROM s"), _count(con, "SELECT count(*) FROM t")
    summary: dict[str, Any] = {"rows_source": rows_s, "rows_target": rows_t, "columns": cols_meta, "keys": r.keys,
                               "key_note": r.key_note, "mode": "key" if r.keys else "row",
                               "unpaired_source": r.unpaired_source, "unpaired_target": r.unpaired_target,
                               "column_mismatches": {}, "notes": list(r.notes)}
    sets: dict[str, int] = {}

    if r.keys:
        kc = [f"c{by_name[k].i}" for k in r.keys]
        vals = [p for p in r.pairs if p.name not in r.keys]
        on = " AND ".join(f"s.{c} = t.{c}" for c in kc)
        order = ", ".join(kc)
        con.execute(f"CREATE TABLE res.missing AS SELECT {out('s')} FROM s ANTI JOIN t ON {on} ORDER BY {', '.join('s.' + c for c in kc)}")
        con.execute(f"CREATE TABLE res.extra AS SELECT {out('t')} FROM t ANTI JOIN s ON {on} ORDER BY {', '.join('t.' + c for c in kc)}")

        def differs(p: Pair) -> str:
            c = f"c{p.i}"
            if o.ignore_nulls:
                return f"(s.{c} IS NOT NULL AND t.{c} IS NOT NULL AND s.{c} <> t.{c})"
            return f"(s.{c} IS DISTINCT FROM t.{c})"

        key_out = ", ".join(f"s.c{by_name[k].i} AS {_q(k)}" for k in r.keys)
        side_by_side = "".join(f", s.c{p.i} AS {_q(p.name + '__source')}, t.c{p.i} AS {_q(p.name + '__target')}" for p in vals)
        any_diff = " OR ".join(differs(p) for p in vals) or "false"
        which = ", ".join(f"CASE WHEN {differs(p)} THEN {_lit(p.name)} END" for p in vals)
        diff_list = f"concat_ws(', ', {which})" if vals else "''"
        con.execute(f"CREATE TABLE res.mismatched AS SELECT {key_out}{side_by_side}, {diff_list} AS _diff_columns "
                    f"FROM s JOIN t ON {on} WHERE {any_diff} ORDER BY {', '.join('s.' + c for c in kc)}")
        per_col = ", ".join(f"count(*) FILTER (WHERE {differs(p)})" for p in vals)
        joined = con.execute(f"SELECT count(*){', ' + per_col if per_col else ''} FROM s JOIN t ON {on}").fetchone()
        summary["column_mismatches"] = {p.name: int(n or 0) for p, n in zip(vals, joined[1:]) if n}
        sets["mismatched"] = _count(con, "SELECT count(*) FROM res.mismatched")
        summary["matched"] = int(joined[0]) - sets["mismatched"]
        null_key = " OR ".join(f"{c} IS NULL" for c in kc)
        summary["null_keys_source"] = _count(con, f"SELECT count(*) FROM s WHERE {null_key}")
        summary["null_keys_target"] = _count(con, f"SELECT count(*) FROM t WHERE {null_key}")
        if summary["null_keys_source"] or summary["null_keys_target"]:
            summary["notes"].append("Rows with an empty (NULL) key never match, so they count as missing / extra.")
        if o.check_duplicates:
            for side, tbl in (("source", "s"), ("target", "t")):
                con.execute(f"CREATE TABLE res.dup_{side} AS SELECT {out('x')}, d._count FROM {tbl} x JOIN "
                            f"(SELECT {order}, count(*) AS _count FROM {tbl} GROUP BY ALL HAVING count(*) > 1) d "
                            f"ON {' AND '.join(f'x.{c} = d.{c}' for c in kc)} ORDER BY {', '.join('x.' + c for c in kc)}")
                summary[f"dup_{side}_keys"] = _count(con, f"SELECT count(*) FROM (SELECT {order} FROM {tbl} GROUP BY ALL HAVING count(*) > 1)")
            if summary["dup_source_keys"] or summary["dup_target_keys"]:
                summary["notes"].append("Some keys occur more than once, so those rows are matched with every copy on "
                                        "the other side and the matched / different counts include each combination.")
    else:
        con.execute(f"CREATE TABLE res.missing AS SELECT {out('d')} FROM (SELECT * FROM s EXCEPT ALL SELECT * FROM t) d")
        con.execute(f"CREATE TABLE res.extra AS SELECT {out('d')} FROM (SELECT * FROM t EXCEPT ALL SELECT * FROM s) d")
        summary["matched"] = rows_s - _count(con, "SELECT count(*) FROM res.missing")
        if o.check_duplicates:
            for side, tbl in (("source", "s"), ("target", "t")):
                con.execute(f"CREATE TABLE res.dup_{side} AS SELECT {out('x')}, count(*) AS _count FROM {tbl} x "
                            f"GROUP BY ALL HAVING count(*) > 1")
                summary[f"dup_{side}_keys"] = _count(con, f"SELECT count(*) FROM res.dup_{side}")

    for name in ("missing", "extra", "dup_source", "dup_target"):
        exists = con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = 'res' AND table_name = ?",
                             [name]).fetchone()[0]
        if exists:
            sets[name] = _count(con, f"SELECT count(*) FROM res.{name}")
    summary["sets"] = sets
    summary["missing"], summary["extra"] = sets["missing"], sets["extra"]
    summary["mismatched"] = sets.get("mismatched", 0)
    summary["dup_source_rows"], summary["dup_target_rows"] = sets.get("dup_source", 0), sets.get("dup_target", 0)
    summary["identical"] = not (summary["missing"] or summary["extra"] or summary["mismatched"])
    return summary


# ---- running one comparison --------------------------------------------------------------

def folder(cid: uuid.UUID | str) -> Path:
    return Path(get_settings().compare_dir) / str(cid)


def results_path(cid: uuid.UUID | str) -> Path:
    return folder(cid) / "results.duckdb"


def _update(cid: uuid.UUID, **fields: Any) -> bool:
    """Write progress / state; returns whether a cancel was requested."""
    with get_sessionmaker()() as s:
        c = s.get(Comparison, cid)
        if c is None:
            return True
        for k, v in fields.items():
            setattr(c, k, v)
        s.commit()
        return bool(c.cancel_requested)


def run_comparison(cid: uuid.UUID | str) -> str:
    cid = uuid.UUID(str(cid))
    with get_sessionmaker()() as s:
        c = s.get(Comparison, cid)
        if c is None or c.status != "queued":
            return "skipped"
        c.status, c.started_at, c.heartbeat_at, c.phase = "running", utcnow(), utcnow(), "Opening both sides"
        s.commit()
        spec = CompareIn(**c.spec)
    work = folder(cid)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with get_sessionmaker()() as s:
            r = resolve(s, spec, work)
        summary = _execute(cid, spec, r, work)
        summary["seconds"] = round(time.monotonic() - started, 2)
        summary["options"] = spec.options.model_dump()
        _update(cid, status="succeeded", phase="Done", summary=summary, finished_at=utcnow(), heartbeat_at=utcnow())
        from ..alerts.engine import comparison_finished

        comparison_finished(cid)  # never raises
        return "succeeded"
    except Cancelled:
        shutil.rmtree(work, ignore_errors=True)
        _update(cid, status="cancelled", phase="Cancelled", finished_at=utcnow())
        return "cancelled"
    except Exception as e:  # noqa: BLE001 - any failure ends the comparison with a readable message
        if not isinstance(e, JobError):
            log.exception("comparison %s failed", cid)
        shutil.rmtree(work, ignore_errors=True)
        _update(cid, status="failed", phase="Failed", error=str(e) if isinstance(e, JobError) else short_error(e),
                finished_at=utcnow())
        return "failed"
    finally:
        prune()


def _execute(cid: uuid.UUID, spec: CompareIn, r: Resolved, work: Path) -> dict[str, Any]:
    work_db, res_db = work / "work.duckdb", results_path(cid)
    con = duckdb.connect(str(work_db))
    try:
        con.execute("SET preserve_insertion_order = false")
        names = [f"c{p.i}" for p in r.pairs]  # pair i is c<i> on both sides: the SQL never quotes user names
        inputs = [StageInput("src", r.src, spec.source, [p.source for p in r.pairs], names, "the source"),
                  StageInput("tgt", r.tgt, spec.target, [p.target for p in r.pairs], names, "the target")]
        try:
            res = staging.stage(con, inputs, chunk=spec.options.read_chunk, tick=lambda n: _update(
                cid, rows_source=n["src"], rows_target=n["tgt"], heartbeat_at=utcnow(), phase="Reading both sides"))
        except staging.StagingCancelled as e:
            raise Cancelled() from e
        except staging.StagingError as e:
            raise JobError(str(e)) from e
        if _update(cid, rows_source=res.rows["src"], rows_target=res.rows["tgt"], heartbeat_at=utcnow(), phase="Comparing"):
            raise Cancelled()
        con.execute(f"ATTACH {_lit(str(res_db))} AS res")
        summary = _diff(con, r, spec)
        con.execute("DETACH res")
        return summary
    finally:
        con.close()
        work_db.unlink(missing_ok=True)
        Path(str(work_db) + ".wal").unlink(missing_ok=True)
        shutil.rmtree(work / "remote", ignore_errors=True)  # downloaded remote sides: read into DuckDB, not needed now


# ---- housekeeping / reading state ----------------------------------------------------------

def prune() -> None:
    """Keep only the latest few finished comparisons (no history), with their result files."""
    keep = current_settings().keep_comparisons
    with get_sessionmaker()() as s:
        old = list(s.scalars(select(Comparison).where(Comparison.status.in_(FINISHED))
                             .order_by(Comparison.finished_at.desc().nullslast(), Comparison.created_at.desc()).offset(keep)))
        for c in old:
            shutil.rmtree(folder(c.id), ignore_errors=True)
            s.delete(c)
        s.commit()


def is_stalled(c: Comparison) -> bool:
    hb = c.heartbeat_at
    if c.status != "running" or hb is None:
        return False
    if hb.tzinfo is None:  # SQLite hands back naive datetimes
        hb = hb.replace(tzinfo=timezone.utc)
    return (utcnow() - hb).total_seconds() > 90


def snapshot(c: Comparison) -> dict[str, Any]:
    return {
        "id": str(c.id), "name": c.name, "status": "stalled" if is_stalled(c) else c.status, "phase": c.phase,
        "rows_source": c.rows_source, "rows_target": c.rows_target, "summary": c.summary, "error": c.error,
        "spec": c.spec, "job_id": str(c.job_id) if c.job_id else None, "created_at": c.created_at,
        "started_at": c.started_at, "finished_at": c.finished_at,
    }
