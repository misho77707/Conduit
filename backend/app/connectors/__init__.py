from typing import Any

from .base import ColumnInfo, Connector, SplitCandidate, TableInfo, TableSchema, TestResult
from .ch import ClickHouseConnector
from .duckdb_db import DuckDBConnector
from .mongo import MongoConnector
from .mysql_db import MySQLConnector
from .pg import PostgresConnector
from .sqlite_db import SQLiteConnector

REGISTRY: dict[str, type[Connector]] = {
    c.type: c
    for c in (PostgresConnector, ClickHouseConnector, MySQLConnector, SQLiteConnector, DuckDBConnector, MongoConnector)
}


class UnknownConnectorType(KeyError):
    pass


def get_connector(type_: str, config: dict[str, Any], secrets: dict[str, Any], timeout: int = 8) -> Connector:
    try:
        cls = REGISTRY[type_]
    except KeyError:
        raise UnknownConnectorType(type_) from None
    return cls(config, secrets, timeout=timeout)


def db_types() -> list[dict[str, Any]]:
    return [c.describe() for c in REGISTRY.values()]


__all__ = [
    "Connector", "TableInfo", "TableSchema", "ColumnInfo", "SplitCandidate", "TestResult",
    "REGISTRY", "get_connector", "db_types", "UnknownConnectorType",
]
