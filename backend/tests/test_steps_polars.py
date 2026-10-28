"""Python steps on the Polars engine (engine/steps.py): the step function, its results' types, its errors, and the
engine choice through the pipelines API. No database needed."""
import datetime as dt
import decimal
import re
import sqlite3
from pathlib import Path

import pyarrow as pa
import pytest

from app.engine.arrowconv import plain_table
from app.engine.steps import Step, StepError, apply_steps, infer_colspecs
from jobhelpers import add_conn, uniq, wait_job


def _pl(code: str) -> Step:
    return Step("s1", "python", {"code": code, "engine": "polars"})


def _typed() -> pa.Table:
    return pa.table({
        "id": pa.array([1, 2, 3], pa.int64()),
        "small": pa.array([1, None, 3], pa.int16()),
        "amount": pa.array([decimal.Decimal("1.50"), None, decimal.Decimal("-2.25")], pa.decimal128(12, 2)),
        "name": pa.array(["ann", None, "Bob"]),
        "ok": pa.array([True, False, None]),
        "day": pa.array([dt.date(2024, 2, 29), None, dt.date(2024, 3, 1)]),
        "at": pa.array([dt.datetime(2024, 1, 1, 12), None, dt.datetime(2024, 1, 2)], pa.timestamp("us", tz="UTC")),
        "raw": pa.array([b"\x00\x01", None, b"x"]),
    })


def test_an_untouched_frame_comes_back_exactly():
    t = _typed()
    out = apply_steps([_pl("def transform(df):\n    return df\n")], t)
    assert out.schema == t.schema and out.equals(t)  # no large_string / large_binary, no metadata


def test_expressions_and_the_pre_imported_names():
    code = ("def transform(df):\n"
            "    return df.with_columns(\n"
            "        pl.col('name').str.to_uppercase().alias('upper'),\n"
            "        (pl.col('amount') * 2).alias('double'),\n"
            "        pl.col('at').dt.year().alias('year'),\n"
            "        pl.when(pl.col('id') > 1).then(pl.lit('big')).otherwise(pl.lit('small')).alias('size'),\n"
            "        pl.lit(np.int64(7)).alias('seven'),\n"
            "    ).filter(pl.col('id') != 3)\n")
    out = apply_steps([_pl(code)], _typed())
    assert out.column("upper").to_pylist() == ["ANN", None]
    assert out.column("double").to_pylist() == [decimal.Decimal("3.00"), None]
    assert out.column("year").to_pylist() == [2024, None]
    assert out.column("size").to_pylist() == ["small", "big"]
    assert out.column("seven").to_pylist() == [7, 7]
    assert out.schema.field("upper").type == pa.string()


def test_a_lazy_frame_is_collected():
    code = "def transform(df):\n    return df.lazy().select('id', (pl.col('id') * 10).alias('x')).filter(pl.col('x') > 10)\n"
    out = apply_steps([_pl(code)], _typed())
    assert out.to_pydict() == {"id": [2, 3], "x": [20, 30]}


def test_library_layouts_come_back_as_plain_arrow():
    code = ("def transform(df):\n"
            "    return df.select(\n"
            "        pl.col('name').cast(pl.Categorical).alias('cat'),\n"
            "        pl.col('name').str.split('n').alias('parts'),\n"
            "        pl.struct('id', 'name').alias('both'),\n"
            "    )\n")
    out = apply_steps([_pl(code)], _typed())
    assert out.schema.field("cat").type == pa.string()
    assert out.schema.field("parts").type == pa.list_(pa.string())
    assert out.schema.field("both").type == pa.struct([pa.field("id", pa.int64()), pa.field("name", pa.string())])
    assert out.schema.metadata is None and all(f.metadata is None for f in out.schema)
    assert out.column("cat").to_pylist() == ["ann", None, "Bob"]
    assert [c.name for c in infer_colspecs(out)] == ["cat", "parts", "both"]


def test_plain_table_leaves_plain_tables_alone():
    t = _typed()
    assert plain_table(t) is not None and plain_table(t).equals(t)
    big = pa.table({"s": pa.array(["a"], pa.large_string()), "d": pa.array(["x"]).dictionary_encode()})
    assert plain_table(big).schema == pa.schema([("s", pa.string()), ("d", pa.string())])


@pytest.mark.parametrize("code, msg", [
    ("x = 1\n", "must define def transform"),
    ("def transform(df):\n    return 'nope'\n", "must return a polars DataFrame or LazyFrame (it returned str)"),
    ("def transform(df):\n    return df['id']\n", "returned a Series"),
    ("def transform(df):\n    return df.select(pl.col('nope'))\n", "Python step failed: ColumnNotFoundError"),
    ("def transform(df):\n    return df.lazy().select(pl.col('nope'))\n", "Python step failed: ColumnNotFoundError"),
    ("def transform(df):\n    return pd.DataFrame()\n", "Python step failed: NameError: name 'pd' is not defined"),
    ("def transform(df:\n", "does not parse"),
])
def test_clear_errors(code, msg):
    with pytest.raises(StepError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        apply_steps([_pl(code)], _typed())


def test_the_engine_is_checked_and_defaults_to_pandas():
    pandas_code = "def transform(df):\n    df['x'] = df['id'] * 2\n    return df\n"
    out = apply_steps([Step("s1", "python", {"code": pandas_code})], _typed())  # no engine: pandas, as before
    assert out.column("x").to_pylist() == [2, 4, 6]
    with pytest.raises(StepError, match="engine must be one of: pandas, polars"):
        apply_steps([Step("s1", "python", {"code": pandas_code, "engine": "spark"})], _typed())


# ---- the editor's Polars catalog, examples and cheat sheet against the installed Polars ---------------------

_FRONT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "pipelines"
needs_frontend = pytest.mark.skipif(not (_FRONT / "pyComplete.ts").exists(), reason="the frontend sources are not here")


def _block(text: str, start: str) -> str:
    i = text.index(start)
    return text[i:text.index("];", i)]


def _ts_strings(block: str, pattern: str) -> list[str]:
    """Template literals / string fields of a TS block, JS escapes undone (only \\\\ and \\` occur in them)."""
    return [s.replace("\\`", "`").replace("\\\\", "\\") for s in re.findall(pattern, block, re.S)]


@needs_frontend
def test_every_polars_name_the_editor_offers_exists():
    import polars as pl

    ts = (_FRONT / "pyComplete.ts").read_text(encoding="utf-8")
    owners = {
        "PL": pl, "PLDF": pl.DataFrame, "LAZY": pl.LazyFrame, "EXPR": pl.Expr, "PLSTR": pl.col("a").str,
        "PLDT": pl.col("a").dt, "PLLIST": pl.col("a").list, "PLGROUPBY": pl.DataFrame({"a": [1]}).group_by("a"),
        "PLSERIES": pl.Series, "WHEN": pl.when(True), "THEN": pl.when(True).then(1),
    }
    missing, seen = [], 0
    for const, owner in owners.items():
        block = _block(ts, f"const {const}: Item[] = [")
        names = re.findall(r"\b[mpf]\(\s*\"([A-Za-z_]\w*)\"", block)
        for listed in re.findall(r"\.\.\.\[([^\]]*)\]\.map", block):  # PL's data types
            names += re.findall(r"\"(\w+)\"", listed)
        seen += len(names)
        missing += [f"{const}.{n}" for n in names if not hasattr(owner, n)]
    assert seen > 200 and not missing, missing


_FIXTURE = {
    "id": [1, 2, 2], "qty": [2, 3, None], "unit_price": [1.5, 2.0, 3.0], "status": ["paid", "open", "paid"],
    "amount": [1500.0, -3.0, 12000.0], "email": [" A@X.io ", None, "b@y.io"], "name": ["ann lee", "bob", None],
    "phone": ["+995 (555) 12", None, "555-1"], "posted_at": ["01.02.2024 10:30", "bad", None],
    "amount_text": ["1,5", "x", None], "created": [dt.datetime(2024, 1, 1, 8), dt.datetime(2024, 2, 29), None],
    "full_name": ["Ann Lee Smith", "Bob", None], "country": ["GE", None, "AM"], "discount": [None, 0.1, 0.2],
    "quantity": [1.0, None, 3.0], "acct": ["a1", "a2", "a1"], "amt": [1.0, 2.0, 3.0], "internal_note": ["x", "y", "z"],
    "currency": ["GEL", "USD", "EUR"], "payload": ['{"user": {"id": 7, "country": "GE"}}', "{}", None],
    "refunded": [False, True, None], "modified": [dt.datetime(2024, 1, 2), dt.datetime(2024, 1, 3), dt.datetime(2024, 1, 1)],
    "rate": [1.0, 2.0, 0.5], "customer_id": ["c1", "c1", "c2"],
    # the cheat sheet's columns
    "price": [1.0, 2.0, 3.0], "tmp": [0, 0, 0], "old": [1, 2, 3], "x": [1, None, 3], "e": [" A ", "b", None],
    "d": ["2024-01-02", "nope", None], "n": ["1.5", "x", None], "first": ["Ann", "Bob", None], "last": ["Lee", "Ray", "X"],
    "s": ["A", "B", None], "a": [1, 2, 3], "b": ["x", "y", "z"],
}


@needs_frontend
def test_the_polars_examples_and_cheat_sheet_run():
    import polars as pl

    tsx = (_FRONT / "PythonGuide.tsx").read_text(encoding="utf-8")
    examples = _ts_strings(_block(tsx, "const POLARS_EXAMPLES"), r"code: `(.*?)`,\n  \}")
    assert len(examples) >= 12
    table = pl.DataFrame(_FIXTURE).to_arrow()
    for code in examples:
        out = apply_steps([_pl(code)], table)
        assert out.num_rows >= 1, code
    cheats = _ts_strings(_block(tsx, "const TO_POLARS"), r"\[\"[^\"]+\", `[^`]*`, `(.*?)`\]")
    assert len(cheats) >= 20
    for snippet in cheats:
        df = pl.DataFrame(_FIXTURE)
        ns = {"pl": pl, "df": df, "f": lambda row: str(row)}
        code = snippet.split("  #")[0]
        exec(code if code.startswith("df") else f"_r = df.select({code})", ns)  # noqa: S102 - our own guide's text


# ---- through the pipelines API ------------------------------------------------------------------

def _sqlite(client, auth, path):
    return add_conn(client, auth, "sqlite", uniq("sqlite"), {"path": str(path), "create_if_missing": True})


def test_a_polars_pipeline_runs_and_switching_engine_needs_a_new_preview(client, auth, env):
    src_path, dst_path = env / "src.sqlite", env / "dst.sqlite"
    with sqlite3.connect(src_path) as c:
        c.execute("create table accounts(acct text, amt real)")
        c.executemany("insert into accounts values (?, ?)", [("a1", 100.0), ("a2", -5.0), ("a3", 250.0)])
    src, dst = _sqlite(client, auth, src_path), _sqlite(client, auth, dst_path)
    code = "def transform(df):\n    return df.filter(pl.col('amt') > 0).with_columns(pl.col('acct').str.to_uppercase())\n"
    steps = [{"id": "s1", "kind": "python", "config": {"code": code, "engine": "polars"}}]
    body = {"name": uniq("pipe"), "source": {"connection_id": src, "table": "accounts"},
            "target": {"connection_id": dst, "table": "out", "mode": "append"}, "options": {"workers": 1}, "steps": steps}
    r = client.post("/api/pipelines", json=body, headers=auth)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    prev = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth)
    assert prev.status_code == 200, prev.text
    job = wait_job(client, auth, client.post(f"/api/pipelines/{pid}/run", headers=auth).json()["id"])
    assert job["status"] == "succeeded" and job["rows_written"] == 2, job
    with sqlite3.connect(dst_path) as c:
        assert sorted(r[0] for r in c.execute("select acct from out")) == ["A1", "A3"]

    pandas_steps = [{"id": "s1", "kind": "python", "config": {"code": code, "engine": "pandas"}}]
    assert client.patch(f"/api/pipelines/{pid}", json={"steps": pandas_steps}, headers=auth).status_code == 200
    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    assert r.status_code == 422 and "preview" in r.text.lower()  # the engine changed: the cached schema is gone

    bad = [{"id": "s1", "kind": "python", "config": {"code": code, "engine": "spark"}}]
    r = client.patch(f"/api/pipelines/{pid}", json={"steps": bad}, headers=auth)
    assert r.status_code == 422 and "pandas or polars" in r.text
