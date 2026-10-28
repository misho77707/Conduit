"""Python steps on the Polars engine (engine/steps.py): the step function, its results' types, its errors, and the
engine choice through the pipelines API. No database needed."""
import datetime as dt
import decimal
import sqlite3

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
