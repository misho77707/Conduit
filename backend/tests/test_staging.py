"""The shared staging module (engine/staging.py): inputs copied in parallel into a scratch DuckDB, typed through the
logical types, with row caps, a guard and errors that name the input."""
import duckdb
import pytest

from app.engine.filesource import FileAdapter, FileSpec
from app.engine.spec import SourceIn
from app.engine.staging import StageInput, StagingCancelled, StagingError, stage
from app.engine.types import ColSpec


def _csv(env, name, rows):
    p = env / name
    p.write_text("id,v\n" + "".join(f"{i},x{i}\n" for i in range(rows)))
    return p


def _inp(env, table, path, **kw):
    a = FileAdapter(FileSpec(path=str(path)))
    return StageInput(table, a, SourceIn(file={"path": str(path)}), a.read_columns(), label=f"input {table}", **kw)


def test_stages_inputs_in_parallel_with_caps(env):
    con = duckdb.connect()
    a = _inp(env, "a", _csv(env, "a.csv", 2500))
    b = _inp(env, "b", _csv(env, "b.csv", 2500), limit=1000, names=["key", "val"])
    seen = []
    res = stage(con, [a, b], chunk=700, tick=lambda n: seen.append(dict(n)) and False)
    assert res.rows == {"a": 2500, "b": 1000} and res.capped == {"b"}
    assert con.execute('select count(*), min(key) from "b"').fetchone() == (1000, "0")
    assert seen and seen[-1] == {"a": 2500, "b": 1000}


def test_guard_errors_pass_through_and_read_errors_name_the_input(env):
    class Full(Exception):
        pass

    def guard(inp):
        raise Full(f"disk full while staging {inp.label}")

    with pytest.raises(Full, match="disk full while staging input a"):
        stage(duckdb.connect(), [_inp(env, "a", _csv(env, "a.csv", 10))], guard=guard)
    broken = _inp(env, "b", _csv(env, "b.csv", 10))
    broken.columns = [ColSpec("nope", "TEXT")]
    with pytest.raises(StagingError, match="Reading input b failed"):
        stage(duckdb.connect(), [broken])


def test_cancel(env):
    with pytest.raises(StagingCancelled):
        stage(duckdb.connect(), [_inp(env, "a", _csv(env, "a.csv", 10))], tick=lambda n: True)
