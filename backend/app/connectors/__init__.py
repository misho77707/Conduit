from typing import Any

from .base import ColumnInfo, Connector, NotADatabase, SplitCandidate, TableInfo, TableSchema, TestResult
from .ch import ClickHouseConnector
from .duckdb_db import DuckDBConnector
from .http import HTTPConnector
from .mongo import MongoConnector
from .mysql_db import MySQLConnector
from .pg import PostgresConnector
from .s3 import S3Connector
from .sftp import SFTPConnector
from .sqlite_db import SQLiteConnector
from .storage import StorageConnector

REGISTRY: dict[str, type[Connector]] = {
    c.type: c
    for c in (PostgresConnector, ClickHouseConnector, MySQLConnector, SQLiteConnector, DuckDBConnector, MongoConnector,
              S3Connector, SFTPConnector, HTTPConnector)
}


class UnknownConnectorType(KeyError):
    pass


def get_connector(type_: str, config: dict[str, Any], secrets: dict[str, Any], timeout: int = 8) -> Connector:
    try:
        cls = REGISTRY[type_]
    except KeyError:
        raise UnknownConnectorType(type_) from None
    return cls(config, secrets, timeout=timeout)


def category_of(type_: str) -> str:
    """"database" or "storage" (S3 / SFTP / HTTP); unknown types count as databases."""
    cls = REGISTRY.get(type_)
    return cls.category if cls else "database"


def db_types() -> list[dict[str, Any]]:
    return [c.describe() for c in REGISTRY.values()]


__all__ = [
    "Connector", "StorageConnector", "TableInfo", "TableSchema", "ColumnInfo", "SplitCandidate", "TestResult",
    "NotADatabase", "REGISTRY", "get_connector", "category_of", "db_types", "UnknownConnectorType",
]
