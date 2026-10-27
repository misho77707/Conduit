import pymysql

from .base import ColumnInfo, Connector, DriverOption, SplitCandidate, TableInfo, TableSchema

_NUMERIC_TYPES = ("tinyint", "smallint", "mediumint", "int", "bigint", "decimal", "numeric", "float", "double")
_TIME_TYPES = ("timestamp", "datetime", "date")
_VERDICT_ORDER = {"recommended": 0, "alternative": 1, "not_advised": 2}


class MySQLConnector(Connector):
    """MySQL and MariaDB (pure-Python driver; both speak the same protocol)."""

    type = "mysql"
    label = "MySQL / MariaDB"
    abbr = "MY"
    kind = "host"
    default_port = 3306
    description = "Relational · 5.7+, MariaDB 10.6+"
    drivers = (
        DriverOption("fast", "Fast: connectorx reads + LOAD DATA writes",
                     "Reads are Arrow from connectorx (Rust); writes stream Arrow-built CSV through LOAD DATA LOCAL INFILE, "
                     "which needs local_infile=ON on the server.", module="connectorx"),
        DriverOption("pymysql", "PyMySQL (classic)", "Rows through Python: every type, slower.", classic=True),
    )

    def cx_uri(self) -> str | None:
        """The connection as a connectorx URI, or None when it uses TLS (connectorx can't take these settings)."""
        from urllib.parse import quote

        c = self.config
        if str(c.get("ssl", "disable")) != "disable":
            return None
        user = quote(str(c.get("user") or ""), safe="")
        pw = quote(str(self.secrets.get("password") or ""), safe="")
        return f"mysql://{user}:{pw}@{c.get('host')}:{int(c.get('port') or self.default_port)}/{quote(str(c.get('database') or ''), safe='')}"

    def _connect(self, **overrides) -> pymysql.connections.Connection:
        c = self.config
        mode = str(c.get("ssl", "disable"))
        kwargs = dict(
            host=c.get("host"), port=int(c.get("port") or self.default_port),
            user=c.get("user") or None, password=self.secrets.get("password") or "",
            database=c.get("database") or None, connect_timeout=self.timeout,
            read_timeout=self.timeout * 4, write_timeout=self.timeout * 4, autocommit=True,
        )
        if mode in ("require", "verify-ca", "verify-full"):
            kwargs["ssl"] = {"check_hostname": mode == "verify-full"}
            kwargs["ssl_verify_cert"] = mode != "require"
            kwargs["ssl_verify_identity"] = mode == "verify-full"
        kwargs.update(overrides)
        return pymysql.connect(**kwargs)

    def _test(self) -> tuple[str, str]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION(), CURRENT_USER(), DATABASE()")
                version, user, db = cur.fetchone()
            flavor = "MariaDB" if "mariadb" in version.lower() else "MySQL"
            return f"{flavor} {version.split('-')[0]}", f"user {user} · database {db or '(none)'}"
        finally:
            conn.close()

    def list_tables(self) -> list[TableInfo]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    # No table_schema filter beyond the system schemas: information_schema.tables is already
                    # scoped by the connected user's privileges, so this naturally lists every database they
                    # can see, not just the one (if any) configured on the connection.
                    "SELECT table_schema, table_name, table_rows, data_length + index_length, engine "
                    "FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                    "AND table_schema NOT IN ('mysql','sys','information_schema','performance_schema') "
                    "ORDER BY table_schema, table_name"
                )
                return [
                    TableInfo(name=f"{s}.{t}", est_rows=int(r) if r is not None else None,
                              size_bytes=int(sz) if sz is not None else None, info=eng or "")
                    for s, t, r, sz, eng in cur.fetchall()
                ]
        finally:
            conn.close()

    def inspect(self, table: str) -> TableSchema:
        schema, _, name = table.partition(".")
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select column_name, data_type, is_nullable, column_key from information_schema.columns "
                    "where table_schema = %s and table_name = %s order by ordinal_position", (schema, name),
                )
                col_rows = cur.fetchall()
                cur.execute(
                    "select column_name, non_unique from information_schema.statistics "
                    "where table_schema = %s and table_name = %s and seq_in_index = 1", (schema, name),
                )
                leading = {col: non_unique == 0 for col, non_unique in cur.fetchall()}
                cur.execute(
                    "select table_rows, data_length + index_length, engine from information_schema.tables "
                    "where table_schema = %s and table_name = %s", (schema, name),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        rows, size, engine = row if row else (None, None, "")

        columns = [
            ColumnInfo(name=col, type=dtype, nullable=(is_null == "YES"), primary_key=(key == "PRI"),
                       indexed=(key != "" or col in leading))
            for col, dtype, is_null, key in col_rows
        ]
        col_meta = {c.name: c for c in columns}
        candidates: list[SplitCandidate] = []
        for col, is_unique in leading.items():
            info = col_meta.get(col)
            if info is None:
                continue
            if info.type.startswith(_NUMERIC_TYPES):
                mode = "id_range"
            elif info.type.startswith(_TIME_TYPES):
                mode = "time_range"
            else:
                continue
            if info.primary_key:
                verdict = "recommended"
                reasons = ["primary key" + (" (InnoDB clustered index)" if engine and engine.lower() == "innodb" else "")]
            elif is_unique:
                verdict, reasons = "alternative", ["unique index"]
            else:
                verdict, reasons = "alternative", ["indexed"]
            candidates.append(SplitCandidate(column=col, mode=mode, verdict=verdict, reasons=reasons))
        notes = [] if candidates else ["No indexed numeric or timestamp column: each split would scan the full table."]
        candidates.sort(key=lambda c: _VERDICT_ORDER[c.verdict])
        return TableSchema(name=table, columns=columns, row_estimate=(int(rows) if rows is not None else None),
                            size_bytes=(int(size) if size is not None else None), candidates=candidates, notes=notes)
