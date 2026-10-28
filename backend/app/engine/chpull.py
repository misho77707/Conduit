"""ClickHouse pulls directly: ClickHouse reads a PostgreSQL / MySQL table itself, no row passes through Conduit.

One split = `INSERT INTO <staging> SELECT CAST(…) … FROM postgresql(<named collection>, schema = …, table = …)
WHERE <split range>`, run by ClickHouse, followed by `MOVE PARTITION … TO TABLE <target>` for each partition of the
staging table (`CREATE TABLE staging AS target`, so the two always match). A moved partition leaves the staging
table, so the move is idempotent: a split that fails while pulling drops its staging table and pulls again; one
that fails while moving moves what is left. Progress is read from system.processes; cancel is KILL QUERY.

Credentials: only a named collection a ClickHouse admin created is used (its name is the source connection's
`ch_collection`, the host:port ClickHouse uses optionally `ch_address`). Conduit never creates one: ClickHouse 24.8
logs CREATE NAMED COLLECTION with the password in plain text in system.query_log. The SQL here names the collection
and an optional host / port override, never a secret.

Values follow the table function's type mapping and a CAST to each target column's type. The method is not offered
where that is known to differ from Conduit's own path: custom SQL, WHERE filters (source dialect), date formats,
pipeline steps, ctid splits, array columns, and timestamptz / TIMESTAMP columns on a source whose time zone isn't UTC
(see the adapters' ch_* hooks).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from sqlalchemy import update

from ..connectors.base import UserFacingError
from ..db import get_engine, get_sessionmaker
from ..models import JobSplit
from .adapters.ch import lit
from .arrowconv import conform
from .ddl import qi, qualified, split_name
from .model import ReadRequest, SplitSpec, dec, enc
from .transform import NO_FILL
from .types import ColSpec, clean, to_dialect

log = logging.getLogger("conduit.engine")

METHOD = "clickhouse_pull"
LIMITS = [
    "No reject file: a value the target refuses fails its split, and nothing of that split is kept.",
    "Values follow ClickHouse's postgresql() / mysql() type mapping, then a CAST to each target column's type; JSON "
    "is written as the source database prints it.",
    "A NULL headed for a non-Nullable target column fails the split instead of becoming a reject.",
    "While a split runs, its rows written are the rows ClickHouse has read so far; they reach the target table when "
    "that split finishes.",
]
PULL_TIMEOUT_S = 24 * 3600
STAGE_PREFIX = "conduit_pull_"
_MOVING = "pull:moving:"  # split resume value while its staged rows are being moved (+ the watermark value, JSON)
# session_timezone: naive date-times are read as UTC whatever the server's zone. prefer_column_name_to_alias: the
# split range's `id` is the source column, not the select list's `CAST(…) AS id` - else ClickHouse can't push the
# range down and every split reads the whole table.
_BASE_SETTINGS = {"session_timezone": "UTC", "prefer_column_name_to_alias": 1}


class PullError(UserFacingError):
    """ClickHouse refused or failed a pull statement (the message is ClickHouse's, without the HTTP wrapper)."""


def ch_message(e: BaseException) -> str:
    text = " ".join(str(e).split())
    if "DB::Exception: " in text:
        text = text.split("DB::Exception: ", 1)[1].split(" (version ", 1)[0]
    return text[:500]


# ---- is it offered ----------------------------------------------------------------------

def _collection(sconn) -> tuple[str, str]:
    cfg = sconn.config or {}
    return str(cfg.get("ch_collection") or "").strip(), str(cfg.get("ch_address") or "").strip()


def target_engine(tgt, table: str) -> str | None:
    schema, name = split_name(table)
    rows = tgt._q("SELECT engine FROM system.tables WHERE database = %s AND name = %s", [schema or tgt._db(), name])
    return rows[0][0] if rows else None


def engine_problem(engine: str) -> str | None:
    if "MergeTree" not in engine or engine.startswith(("Replicated", "Shared")):
        return (f"the target table is {engine}; the pull moves finished partitions in, which needs a MergeTree-family "
                "table that isn't replicated")
    return None


def assess(spec, src, sconn, tgt, out, req: ReadRequest, existing: bool) -> dict[str, Any] | None:
    """The transfer-method choice the review shows: None when it doesn't apply (target not ClickHouse, or a source
    ClickHouse can't read), else whether ClickHouse can pull this job directly and, if not, why."""
    if getattr(tgt, "dialect", "") != "clickhouse" or sconn is None or not getattr(src, "ch_function", None):
        return None
    try:
        why = _why_not(spec, src, sconn, tgt, out, req, existing)
    except Exception as e:  # noqa: BLE001 - a check query failed: say so instead of guessing
        why = f"could not check the source or target ({' '.join(str(e).split())[:200]})"
    coll = _collection(sconn)[0]
    return {"used": "conduit", "eligible": why is None, "why": why, "collection": coll or None, "limits": LIMITS}


def _why_not(spec, src, sconn, tgt, out, req: ReadRequest, existing: bool) -> str | None:
    s, o = spec.source, spec.options
    fn = src.ch_function
    if spec.steps:
        return "pipeline steps run in Conduit, so the rows have to pass through it"
    if not s.table:
        return f"the source is custom SQL, and ClickHouse's {fn}() reads tables"
    if s.where.strip():
        return "the WHERE filter is written in the source database's SQL, but ClickHouse would be the one evaluating it"
    fmt = next((oc for oc in out if oc.fmt), None)
    if fmt is not None:
        return f"column `{fmt.target.name}` has a date format, which only Conduit's own conversion applies"
    if req.split_col == "ctid":
        return "ctid splits are PostgreSQL's own; split on a key column instead"
    if o.strategy == "watermark" and not any(oc.source == req.split_col for oc in out):
        return f"the watermark column `{req.split_col}` has to be one of the loaded columns"
    if not _collection(sconn)[0]:
        return "the source connection has no ClickHouse named collection for direct pulls (set one in its settings)"
    if existing:
        p = engine_problem(target_engine(tgt, spec.target.table) or "")
        if p:
            return p
    return src.ch_pull_problem(req)


# ---- the plan of one job ------------------------------------------------------------------

@dataclass
class Pull:
    collection: str
    func: str  # the table-function call: postgresql(coll, schema = '…', table = '…'[, host = …, port = …])
    exprs: list[tuple[Any, str]]  # (transform.OutCol, the source value as a ClickHouse expression, before the CAST)
    settings: dict[str, Any]
    key: str | None  # source column test / sample reads narrow on when LIMIT isn't pushed down
    first_key: Any = None
    last_key: Any = None  # the sample's last key (looked up once)
    wm_target: str | None = None  # watermark strategy: the target column whose max is the split's new watermark
    wm_logical: str | None = None

    def narrow(self, n: int) -> list[str]:
        """Conditions limiting a test / sample read to about n rows where the source doesn't get the LIMIT."""
        if self.key is None or self.first_key is None:
            return []
        k = qi("clickhouse", self.key)
        if n <= 1 or self.last_key is None:
            return [f"{k} = {lit(self.first_key)}"]
        return [f"{k} >= {lit(self.first_key)}", f"{k} <= {lit(self.last_key)}"]

    def select_list(self, types: dict[str, str]) -> str:
        parts = []
        for oc, e in self.exprs:
            if oc.fill is not NO_FILL:
                e = f"ifNull({e}, {lit(oc.fill)})"
            parts.append(f"CAST({e} AS {types[oc.target.name]}) AS {qi('clickhouse', oc.target.name)}")
        return ", ".join(parts)


def _func(src, sconn, table: str) -> str:
    coll, addr = _collection(sconn)
    args = dict(src.ch_table_args(table))
    if addr:  # the source as ClickHouse reaches it (another host name, a tunnel ...): overrides the collection's
        m = re.match(r"^\[(.+)\](?::(\d+))?$", addr) or re.match(r"^([^:]+)(?::(\d+))?$", addr)
        args["host"] = m.group(1) if m else addr
        if m and m.group(2):
            args["port"] = int(m.group(2))
    return f"{src.ch_function}({coll}, " + ", ".join(f"{k} = {lit(v)}" for k, v in args.items()) + ")"


def target_types(tgt, table: str, cols: list[ColSpec], keys: list[str]) -> dict[str, str]:
    """ClickHouse type of each target column: the table's own, or what Conduit's DDL would create."""
    schema, name = split_name(table)
    rows = tgt._q("SELECT name, type FROM system.columns WHERE database = %s AND table = %s", [schema or tgt._db(), name])
    if rows:
        return dict(rows)
    return {c.name: to_dialect("clickhouse", c.logical, key=c.name in keys, nullable=c.nullable and c.name not in keys)
            for c in cols}


def prepare_pull(spec, src, sconn, tgt, out, req: ReadRequest) -> Pull:
    """Check what ClickHouse sees through the collection and read one row through it (raises JobError)."""
    from .runner import JobError

    s, t = spec.source, spec.target
    coll = _collection(sconn)[0]
    func = _func(src, sconn, s.table)
    settings = {**_BASE_SETTINGS, **src.ch_settings}
    try:
        seen = {r[0]: r[1] for r in tgt._q(f"DESCRIBE TABLE {func}", None, **settings)}
    except Exception as e:  # noqa: BLE001
        raise JobError(f"ClickHouse can't read {s.table} through the named collection `{coll}`: {ch_message(e)}") from e
    by_name = {c.name: c for c in req.columns}
    for name in by_name:
        if name not in seen:
            raise JobError(f"ClickHouse sees another table through `{coll}`: {s.table} has no column `{name}` there "
                           "(check the collection's database)")
        if seen[name].startswith("Array("):
            raise JobError(f"column `{name}` is an array, which the direct pull doesn't convert (ClickHouse reads a NULL "
                           "array as an empty one)")
    exprs = []
    for oc in out:
        ident = qi("clickhouse", oc.source)
        exprs.append((oc, src.ch_expr(by_name[oc.source], ident) or ident))
    key = None
    if not src.ch_limit_pushdown:  # narrow test / sample reads on a key the source can seek, else they scan it all
        if req.split_col and req.split_col in by_name:
            key = req.split_col
        else:
            key = next((c.name for c in src.c.inspect(s.table).columns if c.primary_key), None)
    wm = next((oc for oc in out if oc.source == req.split_col), None) if spec.options.strategy == "watermark" else None
    pull = Pull(coll, func, exprs, settings, key, wm_target=wm.target.name if wm else None,
                wm_logical=req.split_col_logical)
    if key:
        rows = src._rows(f"SELECT {src.sql.q(key)} FROM {qualified(src.dialect, s.table)} ORDER BY 1 LIMIT 1")
        pull.first_key = rows[0][0] if rows else None
    types = target_types(tgt, t.table, [oc.target for oc in out], t.keys)
    try:
        tgt._q(f"SELECT {pull.select_list(types)} FROM {func}{_where(pull.narrow(1))} LIMIT 1", None, **settings)
    except Exception as e:  # noqa: BLE001
        raise JobError(f"ClickHouse can't pull {s.table} through `{coll}`: {ch_message(e)}") from e
    return pull


def _where(conds: list[str]) -> str:
    return " WHERE " + " AND ".join(conds) if conds else ""


def split_conds(req: ReadRequest, sp: SplitSpec) -> list[str]:
    """The split's range (or the job's id / time bounds) as ClickHouse conditions; pushed down to the source."""
    conds: list[str] = []
    if sp.kind == "range" and sp.column:
        col, lo, hi, excl = sp.column, sp.lo, sp.hi, sp.exclusive_lo
    elif req.split_col and req.mode != "full":
        col, lo, hi, excl = req.split_col, req.lo, req.hi, req.exclusive_lo
    else:
        return conds
    q = qi("clickhouse", col)
    if lo is not None:
        conds.append(f"{q} {'>' if excl else '>='} {lit(lo)}")
    if hi is not None:
        conds.append(f"{q} < {lit(hi)}")
    return conds


def sample(prep, sp: SplitSpec, n: int, src) -> pa.Table:
    """Up to n rows of the first split as ClickHouse would insert them (the plan's preview)."""
    pull, tgt, t = prep.pull, prep.tgt, prep.spec.target
    if pull.key and pull.first_key is not None and pull.last_key is None:
        k, fq = src.sql.q(pull.key), qualified(src.dialect, prep.spec.source.table)
        rows = src._rows(f"SELECT MAX(k) FROM (SELECT {k} AS k FROM {fq} ORDER BY {k} LIMIT {int(n)}) AS conduit_k")
        pull.last_key = rows[0][0] if rows else None
    types = target_types(tgt, t.table, prep.target_cols, t.keys)
    sql = (f"SELECT {pull.select_list(types)} FROM {pull.func}"
           f"{_where(split_conds(prep.req, sp) + pull.narrow(n))} LIMIT {int(n)}")
    client = tgt.c._client(read_timeout=600)
    try:
        table = client.query_arrow(sql, settings=pull.settings)
    finally:
        client.close()
    return conform(table, prep.target_cols)


# ---- running one split --------------------------------------------------------------------

def _save(job_id: uuid.UUID, idx: int, **values: Any) -> None:
    with get_engine().begin() as c:
        c.execute(update(JobSplit).where(JobSplit.job_id == job_id, JobSplit.idx == idx).values(**values))


def staging_name(job_id: uuid.UUID, idx: int) -> str:
    return f"{STAGE_PREFIX}{job_id.hex[:12]}_{idx}"


def _wm_value(v: Any, logical: str | None) -> Any:
    """A max() from ClickHouse as the Python value the classic path saves for this column type."""
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc)
        aware = clean(logical or "") == "TIMESTAMPTZ"
        v = (v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)) if aware else v.replace(tzinfo=None)
    return v


def run_split(ctx, idx: int, sp: SplitSpec) -> None:
    """Pull one split into its staging table, then move it into the target (see the module docstring)."""
    from .runner import Cancelled

    prep = ctx.prep
    pull, tgt, t = prep.pull, prep.tgt, prep.spec.target
    db, _ = split_name(t.table)
    db = db or tgt._db()
    stg_name = staging_name(ctx.job_id, idx)
    stg, target = f"{qi('clickhouse', db)}.{qi('clickhouse', stg_name)}", qualified("clickhouse", t.table)
    with get_sessionmaker()() as s:
        state = dec(s.get(JobSplit, (ctx.job_id, idx)).resume)
    client = tgt.c._client(read_timeout=PULL_TIMEOUT_S)
    try:
        def staged() -> bool:
            return bool(client.query("SELECT count() FROM system.tables WHERE database = {d:String} AND name = {n:String}",
                                     parameters={"d": db, "n": stg_name}).result_rows[0][0])

        moving = isinstance(state, str) and state.startswith(_MOVING)
        if not moving:
            _pull(ctx, idx, sp, client, stg, target)
            if ctx.stop.is_set():
                client.command(f"DROP TABLE IF EXISTS {stg}")
                raise Cancelled()
            wm = None
            if pull.wm_target:
                mx, n = client.query(f"SELECT max({qi('clickhouse', pull.wm_target)}), count() FROM {stg}").result_rows[0]
                wm = _wm_value(mx, pull.wm_logical) if n else None
            state = _MOVING + json.dumps(enc(wm))
            _save(ctx.job_id, idx, resume=enc(state))
        wm = dec(json.loads(state[len(_MOVING):]))
        if staged():  # gone: an earlier attempt moved everything and dropped it before marking the split done
            pids = [r[0] for r in client.query(
                "SELECT DISTINCT partition_id FROM system.parts WHERE database = {d:String} AND table = {n:String} AND active",
                parameters={"d": db, "n": stg_name}).result_rows]
            for pid in pids:  # each move is atomic and takes the partition out of staging: a retry moves what is left
                client.command(f"ALTER TABLE {stg} MOVE PARTITION ID {lit(pid)} TO TABLE {target}")
            client.command(f"DROP TABLE IF EXISTS {stg}")
        _save(ctx.job_id, idx, resume=enc(wm))
    except (Cancelled, PullError):
        raise
    except Exception as e:  # noqa: BLE001 - ClickHouse's message, without the HTTP wrapper
        raise PullError(f"ClickHouse: {ch_message(e)}") from e
    finally:
        client.close()


def _pull(ctx, idx: int, sp: SplitSpec, client, stg: str, target: str) -> None:
    """INSERT … SELECT into a fresh staging table; progress from system.processes, cancel by KILL QUERY."""
    from .runner import Cancelled

    prep = ctx.prep
    pull, tgt, t = prep.pull, prep.tgt, prep.spec.target
    engine = target_engine(tgt, t.table) or ""
    if p := engine_problem(engine):  # a table made from custom DDL: known only now
        raise PullError(p[0].upper() + p[1:])
    types = target_types(tgt, t.table, prep.target_cols, t.keys)
    client.command(f"DROP TABLE IF EXISTS {stg}")  # what a failed attempt staged
    client.command(f"CREATE TABLE {stg} AS {target}")
    _save(ctx.job_id, idx, rows_read=0, rows_written=0)
    qid = f"conduit-{ctx.job_id}-{idx}-{uuid.uuid4().hex[:8]}"
    cols = ", ".join(qi("clickhouse", oc.target.name) for oc, _ in pull.exprs)
    sql = (f"INSERT INTO {stg} ({cols}) SELECT {pull.select_list(types)} FROM {pull.func}"
           f"{_where(split_conds(prep.req, sp))}")
    done = threading.Event()

    def watch() -> None:
        w = tgt.c._client()
        try:
            while not done.wait(1.0):
                if ctx.stop.is_set():
                    w.command(f"KILL QUERY WHERE query_id = {lit(qid)} ASYNC")
                    return
                # written_rows only moves once a whole insert block (~1M rows) is squashed: show the rows read so far
                rows = w.query("SELECT read_rows FROM system.processes WHERE query_id = {q:String}",
                               parameters={"q": qid}).result_rows
                if rows:
                    _save(ctx.job_id, idx, rows_read=int(rows[0][0]), rows_written=int(rows[0][0]))
        except Exception:  # noqa: BLE001 - progress is best effort; the INSERT's own result decides
            log.debug("pull progress", exc_info=True)
        finally:
            w.close()

    watcher = threading.Thread(target=watch, daemon=True, name=f"pull-watch-{idx}")
    watcher.start()
    try:
        client.command(sql, settings={**pull.settings, "query_id": qid, "insert_null_as_default": 0,
                                      "max_partitions_per_insert_block": 0})
    except Exception as e:  # noqa: BLE001
        try:
            client.command(f"DROP TABLE IF EXISTS {stg}")
        except Exception:  # noqa: BLE001
            log.debug("dropping the staging table failed", exc_info=True)
        if ctx.stop.is_set():
            raise Cancelled() from e
        raise PullError(f"ClickHouse: {ch_message(e)}") from e
    finally:
        done.set()
        watcher.join(timeout=10)
    n = int(client.query(f"SELECT count() FROM {stg}").result_rows[0][0])
    _save(ctx.job_id, idx, rows_read=n, rows_written=n)
