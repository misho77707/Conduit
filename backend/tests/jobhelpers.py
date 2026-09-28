"""Shared helpers for load-engine tests: live-database availability, connection setup, waiting for jobs."""
import shutil
import socket
import time
import uuid

import psycopg
import pytest


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


def _docker_socket_open(path: str = "/var/run/docker.sock") -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not port_open(5432), reason="needs local PostgreSQL :5432 (conduit/conduit)")
needs_mysql = pytest.mark.skipif(not port_open(3306), reason="needs local MySQL/MariaDB :3306 (conduit/conduit)")
needs_ch = pytest.mark.skipif(not port_open(8123), reason="needs local ClickHouse :8123 (default user, database conduit)")
needs_docker = pytest.mark.skipif(not (_docker_socket_open() and shutil.which("docker")),
                                  reason="needs a local Docker daemon (/var/run/docker.sock) and the docker CLI")

PG = {"host": "127.0.0.1", "port": 5432, "database": "conduit", "user": "conduit"}
MY = {"host": "127.0.0.1", "port": 3306, "database": "conduit", "user": "conduit"}
CH = {"host": "127.0.0.1", "port": 8123, "database": "conduit", "user": "default"}


def uniq(prefix: str = "t") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def pg_run(sql: str, params=None):
    with psycopg.connect(host=PG["host"], port=PG["port"], dbname=PG["database"], user=PG["user"], password="conduit", autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else None


def add_conn(client, auth, type_: str, name: str, config: dict, secrets: dict | None = None) -> str:
    r = client.post("/api/connections", json={"name": name, "type": type_, "config": config, "secrets": secrets or {}}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_pg(client, auth, name: str | None = None) -> str:
    return add_conn(client, auth, "postgres", name or uniq("pg"), PG, {"password": "conduit"})


def add_my(client, auth, name: str | None = None) -> str:
    return add_conn(client, auth, "mysql", name or uniq("my"), MY, {"password": "conduit"})


def add_ch(client, auth, name: str | None = None) -> str:
    return add_conn(client, auth, "clickhouse", name or uniq("ch"), CH, {})


def spec(src_id, src_table, dst_id, dst_table, **kw) -> dict:
    """Job request with sensible small-test defaults; options/target overrides via options= / target= / source=."""
    options = {"workers": 4, "read_chunk": 500, "write_batch": 500, **kw.pop("options", {})}
    target = {"connection_id": dst_id, "table": dst_table, "mode": "append", **kw.pop("target", {})}
    source = {"connection_id": src_id, "table": src_table, **kw.pop("source", {})}
    return {"source": source, "target": target, "options": options, **kw}


def wait_job(client, auth, job_id: str, timeout: float = 90.0) -> dict:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = client.get(f"/api/jobs/{job_id}", headers=auth).json()
        if last["status"] in ("succeeded", "failed", "cancelled"):
            return last
        time.sleep(0.25)
    raise AssertionError(f"job did not finish in {timeout}s: {last}")


def run(client, auth, body: dict, timeout: float = 90.0) -> dict:
    r = client.post("/api/jobs", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return wait_job(client, auth, r.json()["id"], timeout)
