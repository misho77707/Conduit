"""MongoDB adapter: documents <-> flat rows.

Top-level fields become columns (nested documents and arrays travel as JSON text). `where` is a JSON filter
document, e.g. {"status": "active"}. Splits use sampled quantiles of the split field, so any orderable field
works, including ObjectId `_id` (the default).
"""
from __future__ import annotations

import datetime as dt
import decimal
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
from bson import Binary, Decimal128, ObjectId, json_util
from pymongo import InsertOne, UpdateOne
from pymongo.errors import BulkWriteError

from ..arrowconv import rows_to_table_lenient, table_rows
from ..ddl import TargetOptions
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, normalize
from .base import Adapter, PartialWrite, SqlDialect, Writer, range_label

SAMPLE_SIZE = 5000
INFER_DOCS = 500


def to_cell(v: Any) -> Any:
    """BSON value -> plain Python the Arrow layer understands."""
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, Decimal128):
        return v.to_decimal()
    if isinstance(v, Binary):
        return bytes(v)
    return v


def to_bson(v: Any) -> Any:
    """Python value from an Arrow row -> something BSON can store."""
    if isinstance(v, decimal.Decimal):
        return Decimal128(v)
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    return v


def _parse_filter(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        doc = json_util.loads(text)
    except ValueError as e:
        raise ValueError(f"Filter is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError("Filter must be a JSON object such as {\"status\": \"active\"}")
    return doc


def _logical_for(types: set[str]) -> str:
    types = {t for t in types if t != "nonetype"}
    if not types:
        return "VARCHAR"
    if types <= {"int", "int64"}:
        return "BIGINT"
    if types <= {"int", "int64", "float"}:
        return "DOUBLE"
    if len(types) == 1:
        return normalize("mongodb", next(iter(types)))
    return "TEXT" if types & {"dict", "list"} else "VARCHAR"  # mixed types travel as text


class MongoAdapter(Adapter):
    dialect = "mongodb"
    sql = SqlDialect("mongodb", "", False)  # unused: Mongo has no SQL
    max_writers = 8

    def _coll(self):
        client = self.c._client()
        name = self.c._db_name(client)
        if not name:
            client.close()
            raise ValueError("No database set: add one to the connection or to the URI path")
        return client, client[name]

    def is_splittable(self, logical: str | None) -> bool:
        return True

    # ---- schema ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[ColSpec]:
        client, db = self._coll()
        try:
            coll = db[table]
            docs = list(coll.aggregate([{"$sample": {"size": INFER_DOCS}}])) if coll.estimated_document_count() > INFER_DOCS \
                else list(coll.find().limit(INFER_DOCS))
        finally:
            client.close()
        seen: dict[str, set[str]] = {}
        for d in docs:
            for k, v in d.items():
                seen.setdefault(k, set()).add(type(v).__name__.lower())
        names = sorted(seen, key=lambda k: (k != "_id", k))
        return [ColSpec(k, _logical_for(seen[k]), True) for k in names]

    def query_columns(self, sql: str) -> list[ColSpec]:
        raise NotImplementedError("Custom SQL is not available for MongoDB; pick a collection instead")

    def is_unique(self, table: str, column: str) -> bool:
        return column == "_id"

    # ---- source ------------------------------------------------------------------
    def _filter(self, req: ReadRequest) -> dict[str, Any]:
        f = _parse_filter(req.where)
        if req.split_col and req.mode != "full":
            rng: dict[str, Any] = {}
            if req.lo is not None:
                rng["$gt" if req.exclusive_lo else "$gte"] = req.lo
            if req.hi is not None:
                rng["$lt"] = req.hi
            if rng:
                f = {"$and": [f, {req.split_col: rng}]} if f else {req.split_col: rng}
        return f

    def estimate_rows(self, req: ReadRequest, exact: bool = False) -> tuple[int | None, bool]:
        client, db = self._coll()
        try:
            coll = db[req.table or ""]
            f = self._filter(req)
            if not f and not exact:
                return int(coll.estimated_document_count()), False
            return int(coll.count_documents(f)), True
        finally:
            client.close()

    def key_range(self, req: ReadRequest) -> tuple[Any, Any] | None:
        client, db = self._coll()
        try:
            coll = db[req.table or ""]
            f, col = self._filter(req), req.split_col or "_id"
            lo = next(iter(coll.find(f, {col: 1}).sort(col, 1).limit(1)), None)
            if lo is None or lo.get(col) is None:
                return None
            hi = next(iter(coll.find(f, {col: 1}).sort(col, -1).limit(1)))
            return lo[col], hi[col]
        finally:
            client.close()

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        col = req.split_col
        if not col:
            return [SplitSpec(0, "all documents", "full")]
        rng = self.key_range(req)
        if rng is None:
            return [SplitSpec(0, "all documents (empty)", "full")]
        mn, mx = rng
        cuts: list[Any] = []
        if n > 1:
            client, db = self._coll()
            try:
                coll = db[req.table or ""]
                f = self._filter(req)
                pipe = ([{"$match": f}] if f else []) + [{"$sample": {"size": SAMPLE_SIZE}}, {"$project": {col: 1}}]
                vals = sorted(v for d in coll.aggregate(pipe) if (v := d.get(col)) is not None)
            finally:
                client.close()
            try:
                for i in range(1, n):
                    c = vals[i * len(vals) // n]
                    if c > mn and (not cuts or c > cuts[-1]) and c <= mx:
                        cuts.append(c)
            except (TypeError, IndexError):  # mixed types in the field: cannot order the sample
                cuts = []
        bounds = [mn, *cuts, mx]
        splits = []
        for i in range(len(bounds) - 1):
            last = i == len(bounds) - 2
            splits.append(SplitSpec(
                idx=i, label=range_label(col, bounds[i], bounds[i + 1]) if not last else range_label(col, bounds[i], mx),
                kind="range", column=col, lo=bounds[i], hi=bounds[i + 1], resumable=True,
                extra={"hi_incl": True} if last else {}))  # the last piece includes the maximum itself
        return splits

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        f = _parse_filter(req.where)
        conds: list[dict[str, Any]] = [f] if f else []
        if split.kind == "range" and split.column:
            c = split.column
            conds.append({c: {("$gt" if split.exclusive_lo else "$gte"): split.lo, ("$lte" if split.extra.get("hi_incl") else "$lt"): split.hi}})
            if split.extra.get("resume_after") is not None:
                conds.append({c: {"$gt": split.extra["resume_after"]}})
            if split.extra.get("resume_from") is not None:
                conds.append({c: {"$gte": split.extra["resume_from"]}})
        elif req.split_col and req.mode != "full":
            bounds = self._filter(req)
            conds = [bounds]
        flt = {"$and": conds} if len(conds) > 1 else (conds[0] if conds else {})
        names = [c.name for c in req.columns]
        client, db = self._coll()
        try:
            cur = db[req.table or ""].find(flt, {n: 1 for n in names}).batch_size(batch_rows)
            if split.kind == "range" and split.column:
                cur = cur.sort(split.column, 1)
            docs: list[dict[str, Any]] = []
            for doc in cur:
                docs.append(doc)
                if len(docs) >= batch_rows:
                    yield self._batch(docs, req, split)
                    docs = []
            if docs:
                yield self._batch(docs, req, split)
        finally:
            client.close()

    def _batch(self, docs: list[dict[str, Any]], req: ReadRequest, split: SplitSpec) -> Batch:
        names = [c.name for c in req.columns]
        rows = [tuple(to_cell(d.get(n)) for n in names) for d in docs]
        table, rejects = rows_to_table_lenient(rows, req.columns)
        resume = docs[-1].get(split.column) if split.kind == "range" and split.column else None
        return Batch(table, resume, rejects)

    # ---- target ------------------------------------------------------------------
    def prepare_target(self, table: str, cols: list[ColSpec], mode: str, keys: list[str], opts: TargetOptions,
                       create: bool, execute: bool) -> list[str]:
        client, db = self._coll()
        try:
            exists = table in db.list_collection_names()
            stmts: list[str] = []
            if not exists and not create:
                raise ValueError(f"Target collection {table} does not exist")
            if not exists:
                stmts.append(f'db.createCollection("{table}")')
            if mode == "upsert" and keys and keys != ["_id"]:
                spec = ", ".join(f'"{k}": 1' for k in keys)
                stmts.append(f'db.getCollection("{table}").createIndex({{{spec}}}, {{unique: true}})')
            if exists and mode == "replace":
                stmts.append(f'db.getCollection("{table}").deleteMany({{}})')
            if execute:
                coll = db[table]
                for s in stmts:
                    if s.startswith("db.createCollection"):
                        db.create_collection(table)
                    elif ".createIndex" in s:
                        coll.create_index([(k, 1) for k in keys], unique=True)
                    elif ".deleteMany" in s:
                        coll.delete_many({})
            return stmts
        finally:
            client.close()

    def open_writer(self, table: str, cols: list[ColSpec], mode: str, keys: list[str]) -> Writer:
        client, db = self._coll()
        return MongoWriter(client, db[table], [c.name for c in cols], keys if mode == "upsert" else [])


class MongoWriter(Writer):
    def __init__(self, client, coll, names: list[str], keys: list[str]):
        self.client, self.coll, self.names, self.keys = client, coll, names, keys

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        docs = [{n: to_bson(v) for n, v in zip(self.names, row)} for row in table_rows(table)]
        if self.keys:
            ops = [self._upsert(d) for d in docs]
        else:
            ops = [InsertOne(d) for d in docs]
        try:
            self.coll.bulk_write(ops, ordered=False)
        except BulkWriteError as e:
            errs = e.details.get("writeErrors", [])
            if not errs:
                raise
            raise PartialWrite({int(x["index"]): str(x.get("errmsg", "write error")) for x in errs}) from e

    def _upsert(self, d: dict[str, Any]) -> UpdateOne:
        flt = {k: d[k] for k in self.keys}
        body = {k: v for k, v in d.items() if k not in self.keys and k != "_id"}
        update: dict[str, Any] = {}
        if body:
            update["$set"] = body
        if "_id" in d and "_id" not in self.keys:
            update["$setOnInsert"] = {"_id": d["_id"]}
        if not update:
            update["$setOnInsert"] = {}
        return UpdateOne(flt, update, upsert=True)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
