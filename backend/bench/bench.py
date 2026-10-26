"""Load-engine benchmark: rows/s and CPU for source x target pairs, drivers, worker counts, with / without a split.

Not part of pytest. It runs the real engine in this process (CONDUIT_JOB_RUNNER=thread, a throwaway SQLite app
database) through the API, the way the worker runs jobs, against real database servers. Run it in a container
that reaches them, e.g. the throwaway servers README "Performance" describes:

    docker run --rm --network conduit-bench -v "$PWD/backend:/work" -w /work conduit-test \
        python bench/bench.py --rows 2000000 --label baseline

Sources are generated once per server (bench tables, about 2M rows: ids, timestamps with and without time
zone, numeric, double, integers with nulls, text of varied length, booleans, json, a low-cardinality text);
`--regen` rebuilds them. Each run loads one source into one target (replace mode) and reports rows/s and this
process's CPU% (100 = one core busy; a GIL-bound job stays near 100 whatever the worker count).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # the backend: `app` importable without installing it

KINDS = ("pg", "my", "ch", "sqlite", "duckdb")
SRC_TABLE = {"pg": "bench.src", "my": "conduit.bench_src", "ch": "conduit.bench_src", "sqlite": "bench_src",
             "duckdb": "main.bench_src"}


def tgt_table(kind: str, src: str) -> str:
    return {"pg": f"bench.t_{src}", "my": f"conduit.bench_t_{src}", "ch": f"conduit.bench_t_{src}",
            "sqlite": f"bench_t_{src}", "duckdb": f"main.bench_t_{src}"}[kind]


# ---- the source data, generated on each server -------------------------------------------------------------

def gen_pg(cfg: dict, n: int) -> None:
    import psycopg

    with psycopg.connect(host=cfg["host"], port=cfg["port"], dbname="conduit", user="conduit", password="conduit",
                         autocommit=True) as c:
        c.execute("CREATE SCHEMA IF NOT EXISTS bench")
        c.execute("DROP TABLE IF EXISTS bench.src")
        c.execute("CREATE TABLE bench.src (id bigint PRIMARY KEY, created_at timestamp, event_ts timestamptz, "
                  "amount numeric(12,2), score double precision, qty integer, name varchar(100), note text, "
                  "active boolean, payload jsonb, category text)")
        c.execute(
            "INSERT INTO bench.src SELECT g, timestamp '2024-01-01' + g * interval '15 seconds', "
            "timestamptz '2024-01-01 00:00:00+00' + g * interval '7 seconds', ((g * 7919) %% 10000000) / 100.0, "
            "(g * 31 %% 100000) / 7.0, CASE WHEN g %% 10 = 0 THEN NULL ELSE (g %% 5000)::int END, "
            "substr(md5(g::text) || md5((g + 1)::text), 1, (5 + (g %% 40))::int), "
            "CASE WHEN g %% 5 = 0 THEN NULL ELSE repeat('lorem ipsum ', (1 + (g %% 12))::int) END, g %% 3 = 0, "
            "jsonb_build_object('k', g %% 100, 'tags', jsonb_build_array('a', 'b'), 'v', g), "
            "(ARRAY['red','green','blue','amber','violet'])[(1 + (g %% 5))::int] FROM generate_series(1::bigint, %s) g", [n])
        c.execute("ANALYZE bench.src")


def gen_my(cfg: dict, n: int) -> None:
    import pymysql

    c = pymysql.connect(host=cfg["host"], port=cfg["port"], user="conduit", password="conduit", database="conduit",
                        autocommit=True, read_timeout=3600)
    try:
        cur = c.cursor()
        cur.execute("DROP TABLE IF EXISTS bench_src")
        cur.execute("CREATE TABLE bench_src (id BIGINT PRIMARY KEY, created_at DATETIME(6), event_ts TIMESTAMP(6) NULL, "
                    "amount DECIMAL(12,2), score DOUBLE, qty INT NULL, name VARCHAR(100), note TEXT, active BOOLEAN, "
                    "payload JSON, category VARCHAR(20))")
        cur.execute("SET SESSION cte_max_recursion_depth = 100000000, time_zone = '+00:00'")
        step = 500_000
        for lo in range(1, n + 1, step):
            hi = min(lo + step - 1, n)
            cur.execute(
                "INSERT INTO bench_src WITH RECURSIVE s(g) AS (SELECT %s UNION ALL SELECT g + 1 FROM s WHERE g < %s) "
                "SELECT g, TIMESTAMP('2024-01-01') + INTERVAL g * 15 SECOND, TIMESTAMP('2024-01-01') + INTERVAL g * 7 SECOND, "
                "((g * 7919) %% 10000000) / 100.0, (g * 31 %% 100000) / 7.0, IF(g %% 10 = 0, NULL, g %% 5000), "
                "SUBSTR(CONCAT(MD5(g), MD5(g + 1)), 1, 5 + (g %% 40)), IF(g %% 5 = 0, NULL, REPEAT('lorem ipsum ', 1 + (g %% 12))), "
                "g %% 3 = 0, JSON_OBJECT('k', g %% 100, 'tags', JSON_ARRAY('a', 'b'), 'v', g), "
                "ELT(1 + (g %% 5), 'red', 'green', 'blue', 'amber', 'violet') FROM s", [lo, hi])
        cur.execute("ANALYZE TABLE bench_src")
    finally:
        c.close()


def gen_ch(cfg: dict, n: int) -> None:
    import clickhouse_connect

    c = clickhouse_connect.get_client(host=cfg["host"], port=cfg["port"], username="default", database="conduit")
    try:
        c.command("DROP TABLE IF EXISTS conduit.bench_src")
        c.command("CREATE TABLE conduit.bench_src (id Int64, created_at DateTime64(6), event_ts DateTime64(6, 'UTC'), "
                  "amount Decimal(12,2), score Float64, qty Nullable(Int32), name String, note Nullable(String), "
                  "active Bool, payload String, category LowCardinality(String)) ENGINE = MergeTree ORDER BY id")
        c.command(
            "INSERT INTO conduit.bench_src SELECT g, toDateTime64('2024-01-01 00:00:00', 6) + g * 15, "
            "toDateTime64('2024-01-01 00:00:00', 6, 'UTC') + g * 7, toDecimal64(((g * 7919) % 10000000) / 100.0, 2), "
            "(g * 31 % 100000) / 7.0, if(g % 10 = 0, NULL, toInt32(g % 5000)), "
            "substring(concat(hex(MD5(toString(g))), hex(MD5(toString(g + 1)))), 1, 5 + (g % 40)), "
            "if(g % 5 = 0, NULL, repeat('lorem ipsum ', toUInt64(1 + (g % 12)))), g % 3 = 0, "
            f"concat('{{\"k\": ', toString(g % 100), ', \"tags\": [\"a\", \"b\"], \"v\": ', toString(g), '}}'), "
            f"['red','green','blue','amber','violet'][1 + (g % 5)] FROM (SELECT number + 1 AS g FROM numbers({n}))",
            settings={"max_insert_threads": 4})
    finally:
        c.close()


def gen_sqlite(path: Path, n: int) -> None:
    path.unlink(missing_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE bench_src (id INTEGER PRIMARY KEY, created_at TIMESTAMP, event_ts TIMESTAMP, "
              "amount DECIMAL(12,2), score DOUBLE, qty INTEGER, name VARCHAR(100), note TEXT, active BOOLEAN, "
              "payload TEXT, category TEXT)")
    c.execute(
        "INSERT INTO bench_src WITH RECURSIVE s(g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM s WHERE g < ?) "
        "SELECT g, datetime('2024-01-01', '+' || (g * 15) || ' seconds'), datetime('2024-01-01', '+' || (g * 7) || ' seconds'), "
        "((g * 7919) % 10000000) / 100.0, (g * 31 % 100000) / 7.0, CASE WHEN g % 10 = 0 THEN NULL ELSE g % 5000 END, "
        "substr(hex(randomblob(32)), 1, 5 + (g % 40)), CASE WHEN g % 5 = 0 THEN NULL ELSE "
        "substr('lorem ipsum lorem ipsum lorem ipsum lorem ipsum lorem ipsum lorem ipsum lorem ipsum lorem ipsum "
        "lorem ipsum lorem ipsum lorem ipsum lorem ipsum ', 1, 12 * (1 + g % 12)) END, g % 3 = 0, "
        "json_object('k', g % 100, 'tags', json_array('a', 'b'), 'v', g), "
        "CASE g % 5 WHEN 0 THEN 'red' WHEN 1 THEN 'green' WHEN 2 THEN 'blue' WHEN 3 THEN 'amber' ELSE 'violet' END FROM s",
        [n])
    c.commit()
    c.close()


def gen_duckdb(path: Path, n: int) -> None:
    import duckdb

    path.unlink(missing_ok=True)
    c = duckdb.connect(str(path))
    c.execute(
        "CREATE TABLE main.bench_src AS SELECT g::BIGINT AS id, TIMESTAMP '2024-01-01' + INTERVAL 15 SECOND * g AS created_at, "
        "TIMESTAMPTZ '2024-01-01 00:00:00+00' + INTERVAL 7 SECOND * g AS event_ts, "
        "(((g * 7919) % 10000000) / 100.0)::DECIMAL(12,2) AS amount, (g * 31 % 100000) / 7.0 AS score, "
        "CASE WHEN g % 10 = 0 THEN NULL ELSE (g % 5000)::INTEGER END AS qty, "
        "substr(md5(g::VARCHAR) || md5((g + 1)::VARCHAR), 1, 5 + (g % 40)::INTEGER)::VARCHAR(100) AS name, "
        "CASE WHEN g % 5 = 0 THEN NULL ELSE repeat('lorem ipsum ', 1 + (g % 12)::INTEGER) END AS note, g % 3 = 0 AS active, "
        "'{\"k\": ' || (g % 100) || ', \"tags\": [\"a\", \"b\"], \"v\": ' || g || '}' AS payload, "
        "['red','green','blue','amber','violet'][1 + (g % 5)::INTEGER] AS category FROM range(1, ? + 1) t(g)", [n])
    c.close()


def src_rows(kind: str, cfg: dict, data: Path) -> int:
    """Rows in the generated source (0 if it is missing)."""
    try:
        if kind == "pg":
            import psycopg
            with psycopg.connect(host=cfg["host"], port=cfg["port"], dbname="conduit", user="conduit", password="conduit") as c:
                return c.execute("SELECT count(*) FROM bench.src").fetchone()[0]
        if kind == "my":
            import pymysql
            c = pymysql.connect(host=cfg["host"], port=cfg["port"], user="conduit", password="conduit", database="conduit")
            try:
                cur = c.cursor()
                cur.execute("SELECT count(*) FROM bench_src")
                return cur.fetchone()[0]
            finally:
                c.close()
        if kind == "ch":
            import clickhouse_connect
            c = clickhouse_connect.get_client(host=cfg["host"], port=cfg["port"], username="default", database="conduit")
            return int(c.query("SELECT count() FROM conduit.bench_src").result_rows[0][0])
        if kind == "sqlite":
            return sqlite3.connect(data / "src.sqlite").execute("SELECT count(*) FROM bench_src").fetchone()[0]
        import duckdb
        c = duckdb.connect(str(data / "src.duckdb"), read_only=True)
        try:
            return c.execute("SELECT count(*) FROM main.bench_src").fetchone()[0]
        finally:
            c.close()
    except Exception:  # noqa: BLE001 - not generated yet
        return 0


# ---- one run ---------------------------------------------------------------------------------------------------

@dataclass
class Run:
    src: str
    tgt: str
    workers: int
    split: bool
    source_driver: str | None = None
    target_driver: str | None = None
    method: str | None = None  # a transfer method other than "through Conduit" (later: ClickHouse pulls directly)
    rows: int = 0
    seconds: float = 0.0
    rows_s: float = 0.0
    cpu_pct: float = 0.0  # this process, averaged over the run (100 = one core)
    cpu_peak: float = 0.0  # the busiest 1-second sample
    status: str = ""
    drivers_used: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    error: str | None = None


class CpuSampler:
    def __init__(self):
        import psutil

        self.p, self.samples, self.stop = psutil.Process(), [], threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        prev_t, prev_c = time.monotonic(), self._cpu()
        while not self.stop.wait(1.0):
            t, cpu = time.monotonic(), self._cpu()
            self.samples.append(100 * (cpu - prev_c) / (t - prev_t))
            prev_t, prev_c = t, cpu

    def _cpu(self) -> float:
        ct = self.p.cpu_times()
        return ct.user + ct.system

    def __enter__(self):
        self.t0, self.c0 = time.monotonic(), self._cpu()
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.t.join()
        self.avg = 100 * (self._cpu() - self.c0) / max(time.monotonic() - self.t0, 1e-6)
        self.peak = max(self.samples, default=self.avg)


def run_one(client, auth, conns: dict, r: Run, timeout: float) -> Run:
    options = {"workers": r.workers, "split_column": "id" if r.split else None}
    if r.source_driver:
        options["source_driver"] = r.source_driver
    if r.target_driver:
        options["target_driver"] = r.target_driver
    if r.method:
        options["transfer_method"] = r.method
    body = {"name": f"bench {r.src}->{r.tgt} w{r.workers}{' split' if r.split else ''}",
            "source": {"connection_id": conns[("src", r.src)], "table": SRC_TABLE[r.src]},
            "target": {"connection_id": conns[("tgt", r.tgt)], "table": tgt_table(r.tgt, r.src), "mode": "replace", "create": True},
            "options": options}
    with CpuSampler() as cpu:
        res = client.post("/api/jobs", json=body, headers=auth)
        if res.status_code >= 300:
            r.status, r.error = "refused", res.text[:300]
            return r
        jid = res.json()["id"]
        end = time.monotonic() + timeout
        job = {}
        while time.monotonic() < end:
            job = client.get(f"/api/jobs/{jid}", headers=auth).json()
            if job["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.5)
        else:
            client.post(f"/api/jobs/{jid}/cancel", headers=auth)
            r.status, r.error = "timeout", f"over {timeout:.0f} s"
    import datetime as dt

    started, finished = (dt.datetime.fromisoformat(job[k].replace("Z", "+00:00")) if job.get(k) else None
                         for k in ("started_at", "finished_at"))
    r.status = r.status or job["status"]
    r.rows = int(job.get("rows_written") or 0)
    r.seconds = round((finished - started).total_seconds(), 2) if started and finished else 0.0
    r.rows_s = round(r.rows / r.seconds) if r.seconds else 0.0
    r.cpu_pct, r.cpu_peak = round(cpu.avg), round(cpu.peak)
    plan = job.get("plan") or {}
    r.drivers_used = plan.get("drivers") or {}
    r.notes = plan.get("notes") or []
    if job["status"] == "failed":
        r.error = (job.get("error") or "")[:300]
    return r


# ---- main ------------------------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rows", type=int, default=2_000_000)
    ap.add_argument("--sources", default=",".join(KINDS))
    ap.add_argument("--targets", default=",".join(KINDS))
    ap.add_argument("--variants", default="nosplit:1,split:1,split:4,split:8",
                    help="comma list of split|nosplit:workers")
    ap.add_argument("--drivers", default="",
                    help="semicolon list of src_kind=driver or tgt_kind=driver combos to try per pair, e.g. 'pg=adbc;pg=psycopg'")
    ap.add_argument("--pg", default="conduit-bench-pg:5432")
    ap.add_argument("--my", default="conduit-bench-my:3306")
    ap.add_argument("--ch", default="conduit-bench-ch:8123")
    ap.add_argument("--data", default=str(HERE / "data"), help="where the SQLite / DuckDB files live (kept between runs)")
    ap.add_argument("--regen", action="store_true", help="rebuild the sources even if they exist")
    ap.add_argument("--label", default="run", help="results go to bench/results/<label>.jsonl")
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args()

    servers = {k: {"host": v.split(":")[0], "port": int(v.split(":")[1])} for k, v in (("pg", args.pg), ("my", args.my), ("ch", args.ch))}
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="conduit-bench-"))
    from cryptography.fernet import Fernet

    os.environ.update(CONDUIT_DATABASE_URL=f"sqlite:///{tmp / 'app.db'}", CONDUIT_DATA_DIR=str(data),
                      CONDUIT_ALLOWED_FILE_ROOTS=str(data), CONDUIT_JOB_RUNNER="thread", CONDUIT_REDIS_URL="memory://",
                      CONDUIT_SECRET_KEY=Fernet.generate_key().decode(), CONDUIT_ADMIN_PASSWORD="bench-pass-123",
                      CONDUIT_KEEP_FINISHED_JOBS="1000", CONDUIT_MAX_WORKERS_PER_JOB="64")
    from fastapi.testclient import TestClient

    from app import models  # noqa: F401
    from app.db import Base, get_engine
    from app.main import create_app

    Base.metadata.create_all(get_engine())
    sources, targets = args.sources.split(","), args.targets.split(",")
    for kind in dict.fromkeys(sources):
        have = src_rows(kind, servers.get(kind, {}), data)
        if have == args.rows and not args.regen:
            continue
        t0 = time.monotonic()
        {"pg": lambda: gen_pg(servers["pg"], args.rows), "my": lambda: gen_my(servers["my"], args.rows),
         "ch": lambda: gen_ch(servers["ch"], args.rows), "sqlite": lambda: gen_sqlite(data / "src.sqlite", args.rows),
         "duckdb": lambda: gen_duckdb(data / "src.duckdb", args.rows)}[kind]()
        print(f"generated {kind} source: {src_rows(kind, servers.get(kind, {}), data):,} rows in {time.monotonic() - t0:.0f} s", flush=True)

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    results_path = out / f"{args.label}.jsonl"
    results: list[Run] = []
    with TestClient(create_app()) as client:
        tok = client.post("/api/auth/login", json={"username": "admin", "password": "bench-pass-123"}).json()["access_token"]
        auth = {"Authorization": f"Bearer {tok}"}

        def conn(role: str, kind: str) -> str:
            if kind in ("pg", "my", "ch"):
                s = servers[kind]
                body = {"pg": {"type": "postgres", "config": {**s, "database": "conduit", "user": "conduit"}, "secrets": {"password": "conduit"}},
                        "my": {"type": "mysql", "config": {**s, "database": "conduit", "user": "conduit"}, "secrets": {"password": "conduit"}},
                        "ch": {"type": "clickhouse", "config": {**s, "database": "conduit", "user": "default"}, "secrets": {}}}[kind]
            else:
                ext = "sqlite" if kind == "sqlite" else "duckdb"
                body = {"type": kind, "config": {"path": str(data / f"{'src' if role == 'src' else 'tgt'}.{ext}"),
                                                 "create_if_missing": True}, "secrets": {}}
            res = client.post("/api/connections", json={"name": f"{role}-{kind}", **body}, headers=auth)
            assert res.status_code == 201, res.text
            return res.json()["id"]

        conns = {("src", k): conn("src", k) for k in dict.fromkeys(sources)}
        conns.update({("tgt", k): conn("tgt", k) for k in dict.fromkeys(targets)})
        combos: list[dict] = [{}]
        if args.drivers:
            combos = []
            for part in args.drivers.split(";"):
                combo = {}
                for kv in filter(None, part.split(",")):
                    k, v = kv.split("=")
                    combo[k.strip()] = v.strip()
                combos.append(combo)
        variants = [(v.split(":")[0] == "split", int(v.split(":")[1])) for v in args.variants.split(",")]
        for s in sources:
            for t in targets:
                for combo in combos:
                    for split, w in variants:
                        # "pg=adbc" applies to whichever side is Postgres; "src.pg=..." / "tgt.pg=..." to one side only
                        r = Run(s, t, w, split, source_driver=combo.get(f"src.{s}", combo.get(s)),
                                target_driver=combo.get(f"tgt.{t}", combo.get(t)), method=combo.get("method"))
                        r = run_one(client, auth, conns, r, args.timeout)
                        results.append(r)
                        with results_path.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(asdict(r)) + "\n")
                        drv = "/".join(filter(None, [r.source_driver, r.target_driver, r.method])) or "default"
                        print(f"{s:>6} -> {t:<6} {drv:<14} w{w} {'split' if split else 'whole':<5} "
                              f"{r.rows:>9,} rows {r.seconds:>7.1f} s {r.rows_s:>9,.0f} rows/s cpu {r.cpu_pct:>4.0f}% "
                              f"(peak {r.cpu_peak:.0f}%) {r.status}{' ' + r.error if r.error else ''}", flush=True)

    print("\n| source | target | driver | workers | split | rows/s | CPU % |\n|---|---|---|---|---|---|---|")
    for r in results:
        drv = "/".join(filter(None, [r.source_driver, r.target_driver, r.method])) or "default"
        print(f"| {r.src} | {r.tgt} | {drv} | {r.workers} | {'yes' if r.split else 'no'} | "
              f"{r.rows_s:,.0f}{'' if r.status == 'succeeded' else ' (' + r.status + ')'} | {r.cpu_pct:.0f} |")


if __name__ == "__main__":
    main()
