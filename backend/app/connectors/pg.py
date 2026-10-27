import psycopg

from .base import ColumnInfo, Connector, DriverOption, SplitCandidate, TableInfo, TableSchema, parse_extra

_NUMERIC_TYPES = ("smallint", "integer", "bigint", "numeric", "real", "double precision")
_TIME_TYPES = ("timestamp", "date")


class PostgresConnector(Connector):
    type = "postgres"
    label = "PostgreSQL"
    abbr = "PG"
    kind = "host"
    default_port = 5432
    description = "Relational · v12–17"
    drivers = (
        DriverOption("adbc", "ADBC (Arrow, binary COPY)", "Arrow batches straight from binary COPY, both ways; a few "
                     "column types (arrays, intervals, network addresses ...) still read through psycopg.",
                     module="adbc_driver_postgresql"),
        DriverOption("psycopg", "psycopg (classic)", "Rows through Python: every type, slower.", classic=True),
    )

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(**self.conn_kwargs(), autocommit=True)

    def conninfo(self) -> str:
        """The same connection as a libpq conninfo string (what ADBC takes)."""
        from psycopg.conninfo import make_conninfo

        return make_conninfo(**{k: v for k, v in self.conn_kwargs().items() if v is not None})

    def conn_kwargs(self) -> dict:
        c = self.config
        kwargs = dict(
            host=c.get("host"), port=int(c.get("port") or self.default_port),
            dbname=c.get("database") or "postgres", user=c.get("user") or None,
            password=self.secrets.get("password") or None,
            connect_timeout=self.timeout,
        )
        if c.get("ssl"):
            kwargs["sslmode"] = c["ssl"]
        kwargs.update(parse_extra(c.get("extra")))
        return kwargs

    def _test(self) -> tuple[str, str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SHOW server_version")
            version = f"PostgreSQL {cur.fetchone()[0].split(' ')[0]}"
            cur.execute(
                "SELECT current_user, has_database_privilege(current_user, current_database(), 'CREATE'),"
                " current_database(), (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())"
            )
            user, can_create, db, ssl = cur.fetchone()
            priv = "CREATE granted" if can_create else "no CREATE (read-only)"
            return version, f"user {user} · {priv} on {db} · TLS {'on' if ssl else 'off'}"

    def list_tables(self) -> list[TableInfo]:
        sql = """
            SELECT n.nspname, c.relname, c.reltuples::bigint, pg_total_relation_size(c.oid), c.relkind
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
            ORDER BY n.nspname, c.relname
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            return [
                TableInfo(
                    name=f"{schema}.{name}",
                    est_rows=rows if rows >= 0 else None,  # -1 = never analyzed
                    size_bytes=size,
                    info="partitioned" if kind == "p" else "",
                )
                for schema, name, rows, size, kind in cur.fetchall()
            ]

    def inspect(self, table: str) -> TableSchema:
        schema, _, name = table.partition(".")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select a.attname, format_type(a.atttypid, a.atttypmod), not a.attnotnull "
                "from pg_attribute a join pg_class c on c.oid = a.attrelid "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = %s and c.relname = %s and a.attnum > 0 and not a.attisdropped "
                "order by a.attnum",
                (schema, name),
            )
            col_rows = cur.fetchall()
            cur.execute(
                "select a.attname, bool_or(i.indisprimary), bool_or(i.indisunique) "
                "from pg_index i join pg_class c on c.oid = i.indrelid "
                "join pg_namespace n on n.oid = c.relnamespace "
                "join pg_attribute a on a.attrelid = c.oid and a.attnum = i.indkey[0] "
                "where n.nspname = %s and c.relname = %s group by a.attname",
                (schema, name),
            )
            indexed = {attname: (pk, uniq) for attname, pk, uniq in cur.fetchall()}
            cur.execute(
                "select attname, correlation from pg_stats where schemaname = %s and tablename = %s",
                (schema, name),
            )
            correlation = dict(cur.fetchall())
            cur.execute(
                "select c.reltuples::bigint, pg_total_relation_size(c.oid) "
                "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = %s and c.relname = %s",
                (schema, name),
            )
            row = cur.fetchone()
        rows, size = row if row else (None, None)

        columns = [
            ColumnInfo(name=attname, type=type_, nullable=nullable,
                       primary_key=indexed.get(attname, (False, False))[0], indexed=attname in indexed)
            for attname, type_, nullable in col_rows
        ]
        candidates, notes = [], []
        for attname, (pk, uniq) in indexed.items():
            type_ = next((c.type for c in columns if c.name == attname), "")
            if type_.startswith(_NUMERIC_TYPES):
                mode = "id_range"
            elif type_.startswith(_TIME_TYPES):
                mode = "time_range"
            else:
                continue
            corr = correlation.get(attname)
            reasons = ["primary key" if pk else "unique index" if uniq else "indexed"]
            warnings: list[str] = []
            if corr is None:
                verdict = "recommended" if pk else "alternative"
                warnings.append("no statistics yet: run ANALYZE for a reliable estimate")
            elif abs(corr) >= 0.9:
                verdict = "recommended"
                reasons.append(f"physical order matches (correlation {corr:.2f})")
            elif abs(corr) >= 0.5:
                verdict = "alternative"
                warnings.append(f"moderate physical correlation ({corr:.2f}): splits may overlap pages")
            else:
                verdict = "alternative" if pk else "not_advised"
                warnings.append(f"low physical correlation ({corr:.2f}): each split scans much of the table")
            candidates.append(SplitCandidate(column=attname, mode=mode, verdict=verdict, reasons=reasons, warnings=warnings))
        if not candidates:
            candidates.append(SplitCandidate(
                column="ctid", mode="ctid", verdict="alternative",
                reasons=["physical block ranges: works without an index"],
                warnings=["not tied to any column value: cannot resume from a watermark"],
            ))
            notes.append("No indexed numeric or timestamp column: each split otherwise scans the full table.")
        candidates.sort(key=lambda c: {"recommended": 0, "alternative": 1, "not_advised": 2}[c.verdict])
        return TableSchema(name=table, columns=columns, row_estimate=(rows if rows and rows >= 0 else None),
                            size_bytes=size, candidates=candidates, notes=notes)
