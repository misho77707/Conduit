"""One adapter per database type; `get_adapter(connector)` picks the right one."""
from __future__ import annotations

from ...connectors.base import Connector
from .base import Adapter, PartialWrite, SqlDialect, Writer


def _registry() -> dict[str, type[Adapter]]:
    from .ch import ClickHouseAdapter
    from .duckdb_db import DuckDBAdapter
    from .mongo import MongoAdapter
    from .mysql_db import MySQLAdapter
    from .pg import PostgresAdapter
    from .sqlite_db import SQLiteAdapter

    return {"postgres": PostgresAdapter, "clickhouse": ClickHouseAdapter, "mysql": MySQLAdapter,
            "sqlite": SQLiteAdapter, "duckdb": DuckDBAdapter, "mongodb": MongoAdapter}


def get_adapter(connector: Connector) -> Adapter:
    try:
        return _registry()[connector.type](connector)
    except KeyError:
        raise ValueError(f"No load adapter for connection type {connector.type!r}") from None


__all__ = ["Adapter", "PartialWrite", "SqlDialect", "Writer", "get_adapter"]
