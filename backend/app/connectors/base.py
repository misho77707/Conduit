"""Connector plugin interface.

Every database is one Connector subclass. Step 2 (this file) covers connection testing and
table listing. The reader/writer side (inspect, plan splits, read Arrow batches, write batches)
is added to the same classes in the load-engine step, so files and databases end up behind
one interface: reader -> (transforms) -> writer.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

@dataclass
class TestResult:
    __test__ = False  # not a pytest class

    ok: bool
    latency_ms: float | None = None
    version: str | None = None
    detail: str | None = None
    error: str | None = None
    degraded: bool = False
    learned: dict[str, Any] = field(default_factory=dict)  # config to store, see Connector.learned
    note: str | None = None  # e.g. the connection's fast driver could not be opened (jobs fall back)

    @property
    def status(self) -> str:
        if not self.ok:
            return "err"
        return "warn" if self.degraded else "ok"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status
        return d


@dataclass(frozen=True)
class DriverOption:
    """One way to move data in or out of a database (engine/drivers.py chooses between them per job side).

    `read` / `write`: which side it serves; an option may combine a read and a write method. `classic`: today's
    driver, the fallback, which never fails a capability check. `module`: the package it needs (None: always there).
    """

    id: str
    label: str
    note: str
    read: bool = True
    write: bool = True
    classic: bool = False
    module: str | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "note": self.note, "read": self.read, "write": self.write,
                "classic": self.classic}


@dataclass
class TableInfo:
    name: str  # qualified: schema.table (or collection name)
    est_rows: int | None = None
    size_bytes: int | None = None
    exact: bool = False  # est_rows is an exact count, not a catalog estimate
    info: str = ""  # short hint: engine, sorting key, index summary ...

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ColumnInfo:
    name: str
    type: str  # driver-reported type name, shown as-is
    nullable: bool = True
    primary_key: bool = False
    indexed: bool = False  # leading column of some index (PK counts as indexed too)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SplitCandidate:
    """A column the transfer planner could range-split on, ranked for the review step.

    Built from cheap catalog metadata only (no scans): indexes, physical-order stats where the
    source exposes them, and the like. `verdict` drives the recommended/alternative/not-advised
    grouping the UI shows; `reasons` and `warnings` become its chip text.
    """

    column: str
    mode: str  # id_range | time_range | ctid
    verdict: str  # recommended | alternative | not_advised
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TableSchema:
    name: str
    columns: list[ColumnInfo]
    row_estimate: int | None = None
    exact: bool = False
    size_bytes: int | None = None
    candidates: list[SplitCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. "no indexed key: full scan per split"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact(message: str, secrets: dict[str, Any]) -> str:
    """Remove secret values from driver error text before it is stored or returned."""
    for v in secrets.values():
        if isinstance(v, str) and len(v) >= 3:
            message = message.replace(v, "***")
    return message


def parse_extra(extra: str | dict | None) -> dict[str, str]:
    """Extra options come from a textarea: one key=value per line."""
    if not extra:
        return {}
    if isinstance(extra, dict):
        return {str(k): str(v) for k, v in extra.items()}
    out: dict[str, str] = {}
    for line in str(extra).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class UserFacingError(Exception):
    """An error whose message is already written for the user (shown as-is, without the exception class name)."""


class NotADatabase(UserFacingError, ValueError):
    """A file-storage connection was used where a database is needed."""


def _slow_ms() -> int:
    """A handshake slower than this is reported as "degraded" (a runtime setting, imported late so connectors load
    without the app's models)."""
    from ..runtime import current_settings

    return current_settings().connection_slow_ms


class Connector(ABC):
    type: ClassVar[str]
    label: ClassVar[str]
    abbr: ClassVar[str]
    kind: ClassVar[str]  # host | uri | file | s3 | sftp | http  (drives the add-connection form)
    category: ClassVar[str] = "database"  # database | storage (S3 / SFTP / HTTP: files, not tables)
    default_port: ClassVar[int | None] = None
    description: ClassVar[str] = ""
    secret_fields: ClassVar[tuple[str, ...]] = ("password",)
    # Driver options, fastest first, the classic one last (empty: one driver only, nothing to choose).
    drivers: ClassVar[tuple[DriverOption, ...]] = ()

    def __init__(self, config: dict[str, Any], secrets: dict[str, Any], timeout: int = 8):
        self.config = config
        self.secrets = secrets
        self.timeout = timeout
        # Config values a connection discovered and the caller should store (an SFTP host key: trust on first
        # use). Merged into the saved connection's config; returned to the add dialog for unsaved tests.
        self.learned: dict[str, Any] = {}

    # ---- validation / display ----------------------------------------------------

    @classmethod
    def validate(cls, config: dict[str, Any], secrets: dict[str, Any]) -> list[str]:
        """Return human-readable problems; empty list when the config is usable."""
        errors: list[str] = []
        if cls.kind == "host" and not str(config.get("host", "")).strip():
            errors.append("Host is required")
        return errors

    def display_host(self) -> str:
        """What the UI shows in the 'host / path' column. Never includes credentials."""
        host = self.config.get("host", "")
        port = self.config.get("port")
        return f"{host}:{port}" if port else str(host)

    # ---- behaviour ---------------------------------------------------------------

    def test(self) -> TestResult:
        start = time.perf_counter()
        try:
            version, detail = self._test()
        except Exception as e:  # driver errors are diverse; report them, never raise
            text = str(e) if isinstance(e, UserFacingError) else f"{type(e).__name__}: {e}"
            msg = redact(text.strip(), self.secrets)
            return TestResult(ok=False, error=msg[:400])
        latency = (time.perf_counter() - start) * 1000
        return TestResult(
            ok=True, latency_ms=round(latency, 1), version=version, detail=detail,
            degraded=latency > _slow_ms(), learned=dict(self.learned),
        )

    @abstractmethod
    def _test(self) -> tuple[str, str]:
        """Connect and run a trivial query. Return (server version, detail line)."""

    @abstractmethod
    def list_tables(self) -> list[TableInfo]:
        """Tables/collections with cheap catalog statistics (no full scans)."""

    @abstractmethod
    def inspect(self, table: str) -> TableSchema:
        """Columns, keys/indexes, and ranked split candidates for one table. Catalog queries only."""

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "type": cls.type, "label": cls.label, "abbr": cls.abbr, "kind": cls.kind,
            "default_port": cls.default_port, "description": cls.description,
            "secret_fields": list(cls.secret_fields), "category": cls.category,
            "drivers": [d.public() for d in cls.drivers],
        }
