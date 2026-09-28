import re

import clickhouse_connect

from .base import ColumnInfo, Connector, SplitCandidate, TableInfo, TableSchema, parse_extra

_VERDICT_ORDER = {"recommended": 0, "alternative": 1, "not_advised": 2}


def _ch_mode(type_: str) -> str | None:
    """Classify a ClickHouse column type for range splitting, unwrapping Nullable/LowCardinality."""
    t = type_
    for wrapper in ("LowCardinality(", "Nullable("):
        if t.startswith(wrapper) and t.endswith(")"):
            t = t[len(wrapper):-1]
    if t.startswith(("Date", "DateTime")):
        return "time_range"
    if t.startswith(("Int", "UInt", "Float", "Decimal", "String", "FixedString")):
        return "id_range"
    return None


class ClickHouseConnector(Connector):
    """Uses the HTTP(S) interface (8123 / 8443): it gives us Arrow in and out for the load engine.

    The native protocol port (9000 / 9440) will not work here.
    """

    type = "clickhouse"
    label = "ClickHouse"
    abbr = "CH"
    kind = "host"
    default_port = 8123
    description = "Columnar OLAP · HTTP(S) interface"

    def _client(self, read_timeout: int | None = None, **settings):
        c = self.config
        secure = str(c.get("ssl", "disable")) not in ("", "disable", "false", "off")
        port = int(c.get("port") or (8443 if secure else self.default_port))
        return clickhouse_connect.get_client(
            host=c.get("host"), port=port, username=c.get("user") or "default",
            password=self.secrets.get("password") or "", database=c.get("database") or "default",
            secure=secure, verify=str(c.get("ssl")) == "verify-full",
            connect_timeout=self.timeout, send_receive_timeout=read_timeout or self.timeout * 4,
            settings={**parse_extra(c.get("extra")), **settings},
        )

    def _test(self) -> tuple[str, str]:
        client = self._client()
        try:
            row = client.query("SELECT version(), currentUser(), currentDatabase()").first_row
            return f"ClickHouse {row[0]}", f"user {row[1]} · database {row[2]}"
        finally:
            client.close()

    def list_tables(self) -> list[TableInfo]:
        client = self._client()
        try:
            # No `database =` filter: system.tables is already scoped to what the connected user's RBAC
            # grants let them see, so this naturally lists every database they can see, not just the one
            # (if any) configured on the connection.
            res = client.query(
                "SELECT database, name, total_rows, total_bytes, engine, sorting_key, partition_key "
                "FROM system.tables WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA') "
                "AND NOT is_temporary AND engine NOT LIKE '%View' ORDER BY database, name"
            )
            out = []
            for db, name, rows, size, engine, sort_key, part_key in res.result_rows:
                bits = [engine]
                if sort_key:
                    bits.append(f"ORDER BY {sort_key}")
                if part_key:
                    bits.append(f"PARTITION BY {part_key}")
                # total_rows is exact for MergeTree family and NULL for engines that cannot know it
                out.append(TableInfo(
                    name=f"{db}.{name}", est_rows=rows, size_bytes=size,
                    exact=rows is not None and "MergeTree" in engine, info=" · ".join(bits),
                ))
            return out
        finally:
            client.close()

    def inspect(self, table: str) -> TableSchema:
        db, _, name = table.partition(".")
        client = self._client()
        try:
            cols = client.query(
                "select name, type, is_in_sorting_key from system.columns "
                "where database = {db:String} and table = {t:String} order by position",
                parameters={"db": db, "t": name},
            ).result_rows
            trow = client.query(
                "select engine, sorting_key, partition_key, total_rows, total_bytes from system.tables "
                "where database = {db:String} and name = {t:String}",
                parameters={"db": db, "t": name},
            ).first_row
            n_parts = 0
            if trow and trow[2]:
                n_parts = client.query(
                    "select uniqExact(partition_id) from system.parts where database = {db:String} and table = {t:String} and active",
                    parameters={"db": db, "t": name},
                ).first_row[0]
        finally:
            client.close()
        engine, sorting_key, partition_key, rows, size = trow if trow else ("", "", "", None, None)

        columns = [ColumnInfo(name=n, type=ty, nullable=ty.startswith("Nullable("), indexed=bool(sort_flag))
                   for n, ty, sort_flag in cols]
        col_types = {c.name: c.type for c in columns}
        is_merge_tree = "MergeTree" in engine
        sort_cols = [c.strip() for c in sorting_key.split(",") if c.strip()] if sorting_key else []

        candidates: list[SplitCandidate] = []
        for i, col in enumerate(sort_cols):
            mode = _ch_mode(col_types.get(col, ""))
            if mode is None:
                continue  # not a plain column (an expression) or not a rangeable type
            if i == 0:
                verdict, reasons = ("recommended" if is_merge_tree else "alternative"), ["leading column of the sorting key (ORDER BY)"]
            else:
                verdict, reasons = "alternative", [f"sorting key position {i + 1}: weaker pruning than the leading column"]
            candidates.append(SplitCandidate(column=col, mode=mode, verdict=verdict, reasons=reasons))

        if partition_key:
            m = re.search(r"\(([^()]*)\)", partition_key)  # e.g. toYYYYMM(event_date) -> event_date
            part_col = (m.group(1) if m else partition_key).strip()
            if part_col in col_types and part_col not in {c.column for c in candidates}:
                candidates.append(SplitCandidate(
                    column=part_col, mode=_ch_mode(col_types[part_col]) or "id_range", verdict="alternative",
                    reasons=[f"partition key ({partition_key}): split along partition boundaries"],
                ))

        if partition_key and n_parts >= 2:
            candidates.append(SplitCandidate(
                column="_partition_id", mode="partition", verdict="alternative",
                reasons=[f"{n_parts} partitions ({partition_key}): each split reads whole partitions, no sorting needed"],
                warnings=["not ordered by a key: a split that fails midway restarts instead of resuming"],
            ))

        notes = [] if candidates else ["No ORDER BY key on this table: filtering on any column scans every part."]
        candidates.sort(key=lambda c: _VERDICT_ORDER[c.verdict])
        return TableSchema(
            name=table, columns=columns, row_estimate=rows,
            exact=rows is not None and is_merge_tree, size_bytes=size, candidates=candidates, notes=notes,
        )
