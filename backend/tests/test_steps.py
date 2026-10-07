"""Pipeline transform steps (engine/steps.py): pure unit tests, no database needed."""
import pyarrow as pa
import pytest

from app.engine.steps import Step, StepError, apply_steps, compile_steps, infer_colspecs


def _t(**cols) -> pa.Table:
    return pa.table(cols)


def test_rename():
    out = apply_steps([Step("s1", "rename", {"mapping": {"a": "x"}})], _t(a=[1, 2], b=["p", "q"]))
    assert out.column_names == ["x", "b"]
    assert out.column("x").to_pylist() == [1, 2]


def test_cast():
    out = apply_steps([Step("s1", "cast", {"mapping": {"a": "DOUBLE"}})], _t(a=[1, 2, 3]))
    assert out.column("a").type == pa.float64()
    assert out.column("a").to_pylist() == [1.0, 2.0, 3.0]


def test_filter():
    out = apply_steps([Step("s1", "filter", {"expr": "amount > 0"})], _t(amount=[-1, 0, 5, 10]))
    assert out.column("amount").to_pylist() == [5, 10]


def test_derive():
    out = apply_steps([Step("s1", "derive", {"column": "is_big", "expr": "amount > 100"})], _t(amount=[10, 200]))
    assert out.column_names == ["amount", "is_big"]
    assert out.column("is_big").to_pylist() == [False, True]


def test_split_with_drop_source():
    table = _t(posted_at=["2026-01-05 10:00:00", "2026-02-09 11:30:00"])
    step = Step("s1", "split", {"column": "posted_at", "drop_source": True,
                                "into": [{"name": "posted_date", "expr": "CAST(posted_at AS DATE)"}]})
    out = apply_steps([step], table)
    assert out.column_names == ["posted_date"]
    assert [str(v) for v in out.column("posted_date").to_pylist()] == ["2026-01-05", "2026-02-09"]


def test_split_keeps_source_by_default():
    table = _t(posted_at=["2026-01-05 10:00:00"])
    step = Step("s1", "split", {"column": "posted_at", "into": [{"name": "posted_date", "expr": "CAST(posted_at AS DATE)"}]})
    out = apply_steps([step], table)
    assert out.column_names == ["posted_at", "posted_date"]


def test_sql_step_joins_and_computes():
    table = _t(amount=[10, 20], currency=["USD", "EUR"])
    step = Step("s1", "sql", {"sql": "SELECT *, amount * CASE currency WHEN 'EUR' THEN 1.1 ELSE 1 END AS amount_usd "
                                     "FROM {{ input }}"})
    out = apply_steps([step], table)
    assert out.column("amount_usd").to_pylist() == [10.0, 22.0]


def test_sql_step_without_input_placeholder_rejected():
    with pytest.raises(StepError, match="input"):
        apply_steps([Step("s1", "sql", {"sql": "SELECT 1"})], _t(a=[1]))


def test_sql_step_bad_column_raises_step_error():
    with pytest.raises(StepError):
        apply_steps([Step("s1", "sql", {"sql": "SELECT nope FROM {{ input }}"})], _t(a=[1]))


def test_python_step_transform():
    code = "def transform(df):\n    df['b'] = df['a'] * 2\n    return df\n"
    out = apply_steps([Step("s1", "python", {"code": code})], _t(a=[1, 2, 3]))
    assert out.column("b").to_pylist() == [2, 4, 6]


def test_python_step_has_pd_and_np_without_importing():
    code = ("def transform(df):\n"
            "    df['size'] = np.where(df['a'] > 1, 'big', 'small')\n"
            "    df['d'] = pd.to_datetime(df['s'], errors='coerce')\n"
            "    return df\n")
    out = apply_steps([Step("s1", "python", {"code": code})], _t(a=[1, 2], s=["2024-01-02", "nope"]))
    assert out.column("size").to_pylist() == ["small", "big"]
    assert out.column("d").null_count == 1


def test_python_step_must_define_transform():
    with pytest.raises(StepError, match="transform"):
        apply_steps([Step("s1", "python", {"code": "x = 1\n"})], _t(a=[1]))


def test_python_step_must_return_dataframe():
    code = "def transform(df):\n    return 'nope'\n"
    with pytest.raises(StepError, match="DataFrame"):
        apply_steps([Step("s1", "python", {"code": code})], _t(a=[1]))


def test_python_step_runtime_error_wrapped():
    code = "def transform(df):\n    raise ValueError('boom')\n"
    with pytest.raises(StepError, match="boom"):
        apply_steps([Step("s1", "python", {"code": code})], _t(a=[1]))


def test_rename_rejects_bad_identifier():
    with pytest.raises(StepError):
        apply_steps([Step("s1", "rename", {"mapping": {"a; DROP TABLE x": "b"}})], _t(a=[1]))


def test_filter_needs_expr():
    with pytest.raises(StepError, match="condition"):
        apply_steps([Step("s1", "filter", {})], _t(a=[1]))


def test_chain_of_steps():
    table = _t(acct=["a1", "a2"], amt=[10, -5])
    steps = [
        Step("s1", "rename", {"mapping": {"acct": "account_id"}}),
        Step("s2", "filter", {"expr": "amt > 0"}),
        Step("s3", "derive", {"column": "amt_x10", "expr": "amt * 10"}),
    ]
    out = apply_steps(steps, table)
    assert out.column_names == ["account_id", "amt", "amt_x10"]
    assert out.num_rows == 1
    assert out.column("amt_x10").to_pylist() == [100]


def test_compile_steps_rejects_unknown_kind():
    class Fake:
        id, kind, config = "s1", "not-a-kind", {}

    with pytest.raises(StepError):
        compile_steps([Fake()])


def test_infer_colspecs():
    table = _t(id=pa.array([1, 2], type=pa.int64()), name=["a", "b"], active=[True, False])
    cols = {c.name: c for c in infer_colspecs(table)}
    assert cols["id"].logical == "BIGINT"
    assert cols["name"].logical == "VARCHAR"
    assert cols["active"].logical == "BOOLEAN"
