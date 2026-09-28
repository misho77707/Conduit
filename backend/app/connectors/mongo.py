from pymongo import MongoClient

from .base import ColumnInfo, Connector, SplitCandidate, TableInfo, TableSchema

_TIME_PY_TYPES = ("datetime",)


def mask_uri(uri: str) -> str:
    """Drop user:password@ and the query string so the URI is safe to show.

    Parsed by hand: replica-set URIs list several hosts (h1:27017,h2:27017), which urlsplit rejects.
    """
    scheme, sep, rest = uri.strip().partition("://")
    if not sep:
        return "mongodb://…"
    authority, _, path = rest.partition("/")
    hosts = authority.rsplit("@", 1)[-1]  # last '@' separates userinfo (passwords may contain '@')
    path = path.split("?", 1)[0]
    return f"{scheme}://{hosts}" + (f"/{path}" if path else "")


class MongoConnector(Connector):
    type = "mongodb"
    label = "MongoDB"
    abbr = "MG"
    kind = "uri"
    description = "Document · SRV or standard URI"
    secret_fields = ("uri",)  # the URI usually embeds the password

    @classmethod
    def validate(cls, config, secrets):
        uri = str(secrets.get("uri", "")).strip()
        if not uri:
            return ["Connection string is required"]
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            return ["Connection string must start with mongodb:// or mongodb+srv://"]
        return []

    def display_host(self) -> str:
        return mask_uri(str(self.secrets.get("uri", "")))

    def _client(self) -> MongoClient:
        ms = self.timeout * 1000
        kwargs = dict(serverSelectionTimeoutMS=ms, connectTimeoutMS=ms, socketTimeoutMS=ms * 4)
        if self.config.get("auth_source"):
            kwargs["authSource"] = self.config["auth_source"]
        if self.config.get("tls"):
            kwargs["tls"] = True
        return MongoClient(self.secrets["uri"], **kwargs)

    def _db_name(self, client: MongoClient) -> str | None:
        if self.config.get("database"):
            return self.config["database"]
        try:
            return client.get_default_database().name
        except Exception:
            return None

    def _test(self) -> tuple[str, str]:
        client = self._client()
        try:
            hello = client.admin.command("hello")
            version = client.server_info()["version"]
            if hello.get("setName"):
                topo = f"replica set {hello['setName']} · {len(hello.get('hosts', []))} members"
            else:
                topo = "standalone" if not hello.get("msg") == "isdbgrid" else "sharded cluster"
            return f"MongoDB {version}", topo
        finally:
            client.close()

    def list_tables(self) -> list[TableInfo]:
        client = self._client()
        try:
            # List every database this user can see, not just the one (if any) configured on the connection.
            # MongoDB's listDatabases command automatically narrows to "authorizedDatabases" when the user
            # lacks the cluster-wide listDatabases privilege, so this is already scoped to what they can access.
            try:
                names = [n for n in client.list_database_names() if n not in ("admin", "local", "config")]
            except Exception:
                names = []
            configured = self.config.get("database")
            if configured and configured not in names:
                names.append(configured)
            if not names:
                # listDatabases wasn't permitted and no database is configured: fall back to whatever a
                # single default database resolves to (config, or the URI's own database path segment).
                single = self._db_name(client)
                if not single:
                    raise ValueError("No database set: add one to the connection or to the URI path")
                names = [single]
            out = []
            for name in names:
                db = client[name]
                try:
                    colls = sorted(db.list_collection_names())
                except Exception:
                    continue  # not authorized on this database: skip it, don't fail the whole listing
                for coll in colls:
                    if coll.startswith("system."):
                        continue
                    out.append(TableInfo(name=f"{name}.{coll}", est_rows=db[coll].estimated_document_count()))
            return out
        finally:
            client.close()

    def inspect(self, table: str) -> TableSchema:
        db_name, _, coll_name = table.partition(".")
        client = self._client()
        try:
            coll = client[db_name][coll_name]
            sample = coll.find_one() or {}
            indexes = list(coll.list_indexes())
            count = coll.estimated_document_count()
        finally:
            client.close()

        columns = [ColumnInfo(name=k, type=type(v).__name__, primary_key=(k == "_id")) for k, v in sample.items()]
        # Only single-field ascending/descending indexes are usable for range splitting (not text/geo/hashed).
        leading: dict[str, bool] = {}
        for idx in indexes:
            key = idx.get("key", {})
            if not key:
                continue
            field, direction = next(iter(key.items()))
            if isinstance(direction, str):
                continue
            leading.setdefault(field, bool(idx.get("unique", False)))
        for c in columns:
            if c.name in leading:
                c.indexed = True

        candidates = [SplitCandidate(
            column="_id", mode="time_range", verdict="recommended",
            reasons=["default index; ObjectId embeds a creation timestamp so ranges are naturally time-ordered"],
        )]
        for field, unique in leading.items():
            if field == "_id" or field not in sample:
                continue
            py_type = type(sample[field]).__name__
            mode = "time_range" if py_type in _TIME_PY_TYPES else "id_range"
            candidates.append(SplitCandidate(column=field, mode=mode, verdict="alternative",
                                              reasons=["unique index" if unique else "indexed"]))
        notes = [] if len(candidates) > 1 else [
            "Only the default _id index is available; add an index on a query/time field for finer control."]
        return TableSchema(name=table, columns=columns, row_estimate=count, candidates=candidates, notes=notes)
