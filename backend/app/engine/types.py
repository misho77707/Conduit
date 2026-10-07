"""Logical column types: the common vocabulary between every source and target.

A column type is one of a small set of *logical* names (the same names the wizard's type dropdown shows):

    SMALLINT INTEGER BIGINT FLOAT DOUBLE DECIMAL(p,s) VARCHAR VARCHAR(n) TEXT
    BOOLEAN DATE TIMESTAMP TIMESTAMPTZ BLOB

Sources translate their catalog types into a logical type (`normalize`), the engine moves data as
Arrow tables typed from the logical type (`to_arrow`), and each target renders the logical type as its
own DDL type (`to_dialect`). That is why there is no source x target mapping matrix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pyarrow as pa

LOGICAL_TYPES = [
    "SMALLINT", "INTEGER", "BIGINT", "FLOAT", "DOUBLE", "DECIMAL(12,2)", "VARCHAR", "TEXT",
    "BOOLEAN", "DATE", "TIMESTAMP", "TIMESTAMPTZ", "BLOB",
]

_DEC = re.compile(r"^DECIMAL\((\d+)\s*,\s*(\d+)\)$")
_VAR = re.compile(r"^VARCHAR\((\d+)\)$")


@dataclass
class ColSpec:
    name: str
    logical: str
    nullable: bool = True
    source_name: str | None = None  # original name when the column was renamed
    default: str | None = None  # DEFAULT expression of an existing target column, as the database reports it

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.logical, "nullable": self.nullable, "source_name": self.source_name or self.name}


def clean(logical: str) -> str:
    s = re.sub(r"\s+", "", logical.strip().upper())
    return s


def is_valid(logical: str) -> bool:
    s = clean(logical)
    return s in {t.replace(" ", "") for t in LOGICAL_TYPES} or bool(_DEC.match(s)) or bool(_VAR.match(s))


def decimal_args(logical: str) -> tuple[int, int] | None:
    m = _DEC.match(clean(logical))
    if not m:
        return None
    p, s = int(m.group(1)), int(m.group(2))
    return min(p, 38), min(s, p)


# ---- logical <-> Arrow ----------------------------------------------------------

def to_arrow(logical: str) -> pa.DataType:
    s = clean(logical)
    d = decimal_args(s)
    if d:
        return pa.decimal128(*d)
    if _VAR.match(s):
        return pa.string()
    return {
        "SMALLINT": pa.int16(), "INTEGER": pa.int32(), "BIGINT": pa.int64(),
        "FLOAT": pa.float32(), "DOUBLE": pa.float64(),
        "VARCHAR": pa.string(), "TEXT": pa.string(), "BOOLEAN": pa.bool_(), "DATE": pa.date32(),
        "TIMESTAMP": pa.timestamp("us"), "TIMESTAMPTZ": pa.timestamp("us", tz="UTC"), "BLOB": pa.binary(),
    }.get(s, pa.string())


def from_arrow(t: pa.DataType) -> str:
    if pa.types.is_int8(t) or pa.types.is_int16(t) or pa.types.is_uint8(t):
        return "SMALLINT"
    if pa.types.is_int32(t) or pa.types.is_uint16(t):
        return "INTEGER"
    if pa.types.is_int64(t) or pa.types.is_uint32(t):
        return "BIGINT"
    if pa.types.is_uint64(t):
        return "DECIMAL(20,0)"
    if pa.types.is_float16(t) or pa.types.is_float32(t):
        return "FLOAT"
    if pa.types.is_float64(t):
        return "DOUBLE"
    if pa.types.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_timestamp(t):
        return "TIMESTAMPTZ" if t.tz else "TIMESTAMP"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "BLOB"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "VARCHAR"
    return "TEXT"  # nested / unknown types travel as JSON text


# ---- catalog type strings -> logical -------------------------------------------

def _dec(raw: str, default: str = "DECIMAL(38,10)") -> str:
    m = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\)", raw)
    if not m:
        return default
    return f"DECIMAL({min(int(m.group(1)), 38)},{int(m.group(2) or 0)})"


def _len(raw: str) -> str:
    m = re.search(r"\(\s*(\d+)\s*\)", raw)
    return f"VARCHAR({m.group(1)})" if m else "VARCHAR"


def normalize(dialect: str, raw: str) -> str:
    t = raw.strip()
    low = t.lower()
    return _NORMALIZERS[dialect](t, low)


def _n_postgres(t: str, low: str) -> str:
    if low.endswith("[]") or low.startswith("_"):
        return "TEXT"
    if low.startswith(("timestamp with time zone", "timestamptz")):
        return "TIMESTAMPTZ"
    if low.startswith(("timestamp", "timestamp without")):
        return "TIMESTAMP"
    if low.startswith("numeric") or low.startswith("decimal"):
        return _dec(t)
    if low.startswith(("character varying", "varchar", "character", "char", "bpchar")):
        return _len(t)
    return {
        "smallint": "SMALLINT", "integer": "INTEGER", "int": "INTEGER", "bigint": "BIGINT", "real": "FLOAT",
        "double precision": "DOUBLE", "text": "TEXT", "boolean": "BOOLEAN", "date": "DATE", "bytea": "BLOB",
        "json": "TEXT", "jsonb": "TEXT", "uuid": "VARCHAR",
    }.get(low, "VARCHAR")


def _n_mysql(t: str, low: str) -> str:
    unsigned = "unsigned" in low
    base = re.split(r"[\s(]", low)[0]
    if base == "tinyint":
        return "BOOLEAN" if re.match(r"tinyint\(1\)", low) else "SMALLINT"
    if base == "smallint":
        return "INTEGER" if unsigned else "SMALLINT"
    if base in ("mediumint", "int", "integer"):
        return "BIGINT" if unsigned else "INTEGER"
    if base == "bigint":
        return "DECIMAL(20,0)" if unsigned else "BIGINT"
    if base in ("decimal", "numeric"):
        return _dec(t, "DECIMAL(38,10)")
    if base in ("float",):
        return "FLOAT"
    if base in ("double", "real"):
        return "DOUBLE"
    if base in ("varchar", "char"):
        return _len(t)
    if base in ("datetime", "timestamp"):
        return "TIMESTAMP"
    if base == "date":
        return "DATE"
    if base == "year":
        return "INTEGER"
    if base in ("tinytext", "text", "mediumtext", "longtext", "json", "enum", "set"):
        return "TEXT"
    if base in ("tinyblob", "blob", "mediumblob", "longblob", "binary", "varbinary"):
        return "BLOB"
    return "VARCHAR"


def _n_clickhouse(t: str, low: str) -> str:
    s = t
    while True:
        m = re.match(r"^(?:Nullable|LowCardinality)\((.*)\)$", s)
        if not m:
            break
        s = m.group(1)
    if s.startswith("Decimal"):
        m = re.match(r"^Decimal(?:32|64|128|256)?\(\s*(\d+)(?:\s*,\s*(\d+))?\s*\)", s)
        if m and s.startswith("Decimal("):
            return f"DECIMAL({min(int(m.group(1)), 38)},{int(m.group(2) or 0)})"
        m = re.match(r"^Decimal(32|64|128|256)\(\s*(\d+)\s*\)", s)
        if m:
            p = {"32": 9, "64": 18, "128": 38, "256": 38}[m.group(1)]
            return f"DECIMAL({p},{int(m.group(2))})"
    if s.startswith("DateTime64") or s == "DateTime" or s.startswith("DateTime("):
        return "TIMESTAMPTZ" if re.search(r"'[^']+'", s) else "TIMESTAMP"
    if s.startswith("FixedString"):
        return _len(s)
    return {
        "Int8": "SMALLINT", "Int16": "SMALLINT", "Int32": "INTEGER", "Int64": "BIGINT",
        "UInt8": "SMALLINT", "UInt16": "INTEGER", "UInt32": "BIGINT", "UInt64": "DECIMAL(20,0)",
        "Int128": "DECIMAL(38,0)", "UInt128": "DECIMAL(38,0)", "Float32": "FLOAT", "Float64": "DOUBLE",
        "Bool": "BOOLEAN", "String": "VARCHAR", "Date": "DATE", "Date32": "DATE", "UUID": "VARCHAR",
    }.get(s, "TEXT" if s.startswith(("Array", "Map", "Tuple", "Nested", "JSON", "Object")) else "VARCHAR")


def _n_sqlite(t: str, low: str) -> str:
    if "bool" in low:
        return "BOOLEAN"
    if low.startswith(("datetime", "timestamp")):
        return "TIMESTAMP"
    if low.startswith("date"):
        return "DATE"
    if low.startswith(("decimal", "numeric")):
        return _dec(t, "DOUBLE") if "(" in t else "DOUBLE"
    if "int" in low:
        return "BIGINT"
    if any(k in low for k in ("char", "clob", "text")):
        return "TEXT"
    if "blob" in low:
        return "BLOB"
    if any(k in low for k in ("real", "floa", "doub")):
        return "DOUBLE"
    return "TEXT"


def _n_duckdb(t: str, low: str) -> str:
    if low.startswith("decimal"):
        return _dec(t)
    if low.startswith(("timestamp with time zone", "timestamptz")):
        return "TIMESTAMPTZ"
    if low.startswith("timestamp"):
        return "TIMESTAMP"
    if low.startswith(("struct", "map", "union")) or low.endswith("]") or low == "json":
        return "TEXT"
    if low.startswith("varchar"):
        return _len(t)
    return {
        "tinyint": "SMALLINT", "smallint": "SMALLINT", "utinyint": "SMALLINT", "integer": "INTEGER",
        "usmallint": "INTEGER", "bigint": "BIGINT", "uinteger": "BIGINT", "ubigint": "DECIMAL(20,0)",
        "hugeint": "DECIMAL(38,0)", "uhugeint": "DECIMAL(38,0)", "float": "FLOAT", "double": "DOUBLE",
        "boolean": "BOOLEAN", "date": "DATE", "blob": "BLOB", "uuid": "VARCHAR", "time": "VARCHAR",
    }.get(low, "VARCHAR")


def _n_mongodb(t: str, low: str) -> str:
    return {
        "int": "BIGINT", "int64": "BIGINT", "float": "DOUBLE", "str": "VARCHAR", "bool": "BOOLEAN",
        "datetime": "TIMESTAMP", "decimal128": "DECIMAL(38,10)", "objectid": "VARCHAR", "bytes": "BLOB",
        "dict": "TEXT", "list": "TEXT", "nonetype": "VARCHAR",
    }.get(low, "VARCHAR")


_NORMALIZERS = {
    "postgres": _n_postgres, "mysql": _n_mysql, "clickhouse": _n_clickhouse,
    "sqlite": _n_sqlite, "duckdb": _n_duckdb, "mongodb": _n_mongodb,
}


# ---- logical -> dialect DDL type ------------------------------------------------

def to_dialect(dialect: str, logical: str, *, key: bool = False, nullable: bool = True) -> str:
    s = clean(logical)
    d = decimal_args(s)
    vm = _VAR.match(s)
    n = int(vm.group(1)) if vm else None
    base = "VARCHAR" if vm else ("DECIMAL" if d else s)

    if dialect == "postgres":
        if d:
            return f"numeric({d[0]},{d[1]})"
        if n:
            return f"varchar({n})"
        return {"SMALLINT": "smallint", "INTEGER": "integer", "BIGINT": "bigint", "FLOAT": "real",
                "DOUBLE": "double precision", "VARCHAR": "text", "TEXT": "text", "BOOLEAN": "boolean",
                "DATE": "date", "TIMESTAMP": "timestamp", "TIMESTAMPTZ": "timestamptz", "BLOB": "bytea"}.get(base, "text")

    if dialect == "mysql":
        if d:
            return f"DECIMAL({d[0]},{d[1]})"
        if n:
            return f"VARCHAR({n})"
        return {"SMALLINT": "SMALLINT", "INTEGER": "INT", "BIGINT": "BIGINT", "FLOAT": "FLOAT", "DOUBLE": "DOUBLE",
                "VARCHAR": "VARCHAR(255)" if key else "TEXT", "TEXT": "VARCHAR(255)" if key else "LONGTEXT",
                "BOOLEAN": "TINYINT(1)", "DATE": "DATE", "TIMESTAMP": "DATETIME(6)", "TIMESTAMPTZ": "DATETIME(6)",
                "BLOB": "LONGBLOB"}.get(base, "TEXT")

    if dialect == "clickhouse":
        if d:
            t = f"Decimal({d[0]}, {d[1]})"
        elif n is not None and n <= 8:
            # short codes (currency, country) compress far better this way; Nullable goes INSIDE LowCardinality
            return "LowCardinality(Nullable(String))" if nullable and not key else "LowCardinality(String)"
        else:
            t = {"SMALLINT": "Int16", "INTEGER": "Int32", "BIGINT": "Int64", "FLOAT": "Float32", "DOUBLE": "Float64",
                 "VARCHAR": "String", "TEXT": "String", "BOOLEAN": "Bool", "DATE": "Date",
                 "TIMESTAMP": "DateTime64(6, 'UTC')", "TIMESTAMPTZ": "DateTime64(6, 'UTC')", "BLOB": "String"}.get(base, "String")
        return f"Nullable({t})" if nullable and not key else t

    if dialect == "sqlite":
        # SQLite accepts any type name and only derives an "affinity" from it; keep the real names so the
        # column reads back as DATE / BOOLEAN / DECIMAL(p,s) instead of anonymous TEXT.
        if d:
            return f"DECIMAL({d[0]},{d[1]})"
        if n:
            return f"VARCHAR({n})"
        return {"SMALLINT": "SMALLINT", "INTEGER": "INTEGER", "BIGINT": "BIGINT", "FLOAT": "REAL", "DOUBLE": "DOUBLE",
                "VARCHAR": "TEXT", "TEXT": "TEXT", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
                "TIMESTAMPTZ": "TIMESTAMP", "BLOB": "BLOB"}.get(base, "TEXT")

    if dialect == "duckdb":
        if d:
            return f"DECIMAL({d[0]},{d[1]})"
        if n:
            return f"VARCHAR({n})"
        return {"SMALLINT": "SMALLINT", "INTEGER": "INTEGER", "BIGINT": "BIGINT", "FLOAT": "FLOAT", "DOUBLE": "DOUBLE",
                "VARCHAR": "VARCHAR", "TEXT": "VARCHAR", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
                "TIMESTAMPTZ": "TIMESTAMPTZ", "BLOB": "BLOB"}.get(base, "VARCHAR")

    if dialect == "mongodb":
        return {"SMALLINT": "Int32", "INTEGER": "Int32", "BIGINT": "Long", "FLOAT": "Double", "DOUBLE": "Double",
                "DECIMAL": "Decimal128", "VARCHAR": "String", "TEXT": "String", "BOOLEAN": "Boolean", "DATE": "Date",
                "TIMESTAMP": "Date", "TIMESTAMPTZ": "Date", "BLOB": "BinData"}.get(base, "String")

    raise ValueError(f"Unknown dialect: {dialect}")
