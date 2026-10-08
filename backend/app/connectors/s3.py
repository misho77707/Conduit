"""Amazon S3 and S3-compatible storage (MinIO, Cloudflare R2, Wasabi ...) via boto3."""
from __future__ import annotations

import posixpath
from collections.abc import Iterator
from typing import Any, BinaryIO

from .storage import CHUNK, RangeReader, RemoteEntry, RemoteError, RemoteNotFound, StorageConnector, clean_path


def _code(e: Exception) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) if hasattr(e, "response") else ""


class S3Connector(StorageConnector):
    type = "s3"
    label = "Amazon S3"
    abbr = "S3"
    kind = "s3"
    default_port = None
    description = "Buckets on AWS or any S3-compatible store (MinIO, R2, Wasabi)."
    secret_fields = ("access_key_id", "secret_access_key", "session_token")

    @classmethod
    def validate(cls, config: dict[str, Any], secrets: dict[str, Any]) -> list[str]:
        errors = []
        if not str(config.get("bucket", "")).strip():
            errors.append("Bucket is required")
        if bool(secrets.get("access_key_id")) != bool(secrets.get("secret_access_key")):
            errors.append("Give both the access key ID and the secret access key (or neither, to use the server's role)")
        ep = str(config.get("endpoint_url") or "").strip()
        if ep and not ep.startswith(("http://", "https://")):
            errors.append("Endpoint URL must start with http:// or https://")
        return errors

    # ---- plumbing ------------------------------------------------------------------
    @property
    def bucket(self) -> str:
        return str(self.config.get("bucket", "")).strip()

    @property
    def prefix(self) -> str:
        return clean_path(str(self.config.get("prefix") or ""))

    def _key(self, path: str) -> str:
        p = clean_path(path)
        return f"{self.prefix}/{p}" if self.prefix and p else self.prefix or p

    def _rel(self, key: str) -> str:
        return key[len(self.prefix) + 1:] if self.prefix and key.startswith(self.prefix + "/") else key

    def display_host(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}".rstrip("/")

    def _client(self):
        import boto3
        from botocore.config import Config

        cfg = Config(
            region_name=str(self.config.get("region") or "us-east-1"),
            connect_timeout=self.timeout, read_timeout=max(self.timeout, 30), retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path" if self.config.get("path_style") else "auto"},
        )
        return boto3.client(
            "s3", endpoint_url=str(self.config.get("endpoint_url") or "").strip() or None,
            aws_access_key_id=self.secrets.get("access_key_id") or None,
            aws_secret_access_key=self.secrets.get("secret_access_key") or None,
            aws_session_token=self.secrets.get("session_token") or None, config=cfg,
        )

    def _explain(self, e: Exception, what: str) -> RemoteError:
        code = _code(e)
        msg = {
            "AccessDenied": f"Access denied to bucket {self.bucket}: check the keys and the bucket policy",
            "AllAccessDisabled": f"Access denied to bucket {self.bucket}",
            "InvalidAccessKeyId": "The access key ID is not valid",
            "SignatureDoesNotMatch": "The secret access key is wrong",
            "ExpiredToken": "The session token has expired",
            "NoSuchBucket": f"Bucket {self.bucket} does not exist",
            "PermanentRedirect": f"Bucket {self.bucket} is in another region: set the right region",
            "AuthorizationHeaderMalformed": "The region does not match the bucket's region",
        }.get(code)
        if msg is None and type(e).__name__ in ("EndpointConnectionError", "ConnectTimeoutError", "ConnectionClosedError"):
            msg = f"Could not reach the S3 endpoint: {e}"
        if msg is None and type(e).__name__ == "NoCredentialsError":
            msg = "No credentials: give an access key ID and secret, or run Conduit with an IAM role"
        return RemoteError(msg or f"{what} failed: {code or type(e).__name__}: {e}"[:400])

    # ---- test --------------------------------------------------------------------------
    def _test(self) -> tuple[str, str]:
        client = self._client()
        try:
            res = client.list_objects_v2(Bucket=self.bucket, Prefix=(self.prefix + "/") if self.prefix else "", MaxKeys=1)
        except Exception as e:  # noqa: BLE001
            raise self._explain(e, "Listing the bucket") from e
        region = client.meta.region_name
        found = "objects found" if res.get("KeyCount") else "no objects under this prefix yet"
        return "S3", f"bucket {self.bucket} · region {region} · {found}"

    # ---- filesystem --------------------------------------------------------------------
    def _entry(self, key: str, size: int | None, modified, etag: str | None) -> RemoteEntry:
        rel = self._rel(key)
        return RemoteEntry(name=posixpath.basename(rel), path=rel, size=size, modified=modified,
                           etag=(etag or "").strip('"') or None)

    def list(self, path: str) -> list[RemoteEntry]:
        base = self._key(path)
        pfx = f"{base}/" if base else ""
        client = self._client()
        dirs, files = [], []
        try:
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=pfx, Delimiter="/"):
                for cp in page.get("CommonPrefixes") or []:
                    rel = self._rel(cp["Prefix"].rstrip("/"))
                    name = posixpath.basename(rel)
                    if not name.startswith("."):
                        dirs.append(RemoteEntry(name=name, path=rel, is_dir=True))
                for o in page.get("Contents") or []:
                    if o["Key"] == pfx:  # a "folder" marker object
                        continue
                    e = self._entry(o["Key"], o.get("Size"), o.get("LastModified"), o.get("ETag"))
                    if not e.name.startswith("."):
                        files.append(e)
        except Exception as e:  # noqa: BLE001
            raise self._explain(e, "Listing") from e
        return sorted(dirs, key=lambda d: d.name.lower()) + sorted(files, key=lambda f: f.name.lower())

    def walk(self, folder: str, recursive: bool, limit: int) -> Iterator[RemoteEntry]:
        if not recursive:
            yield from (e for e in self.list(folder) if not e.is_dir)
            return
        base = self._key(folder)
        client = self._client()
        seen = 0
        try:
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"{base}/" if base else ""):
                for o in page.get("Contents") or []:
                    seen += 1
                    if seen > limit:
                        raise RemoteError(f"More than {limit:,} objects under '{folder or '/'}': narrow the pattern's folder")
                    e = self._entry(o["Key"], o.get("Size"), o.get("LastModified"), o.get("ETag"))
                    if not o["Key"].endswith("/") and not any(s.startswith(".") for s in e.path.split("/")):
                        yield e
        except RemoteError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._explain(e, "Listing") from e

    def info(self, path: str) -> RemoteEntry:
        key = self._key(path)
        if not key:
            raise RemoteError("Choose a file, not the bucket root")
        try:
            h = self._client().head_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            if _code(e) in ("404", "NoSuchKey", "NotFound"):
                raise RemoteNotFound(f"{self.display_path(path)} does not exist") from e
            raise self._explain(e, "Reading file details") from e
        return self._entry(key, h.get("ContentLength"), h.get("LastModified"), h.get("ETag"))

    def open(self, path: str) -> BinaryIO:
        key = self._key(path)
        info = self.info(path)
        client = self._client()

        def read_range(start: int, length: int) -> bytes:
            if length <= 0:
                return b""
            try:
                return client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{start + length - 1}")["Body"].read()
            except Exception as e:  # noqa: BLE001
                raise self._explain(e, "Reading") from e

        return RangeReader(info.size or 0, read_range, name=info.name)  # type: ignore[return-value]

    def stream(self, path: str) -> Iterator[bytes]:
        try:
            body = self._client().get_object(Bucket=self.bucket, Key=self._key(path))["Body"]
        except Exception as e:  # noqa: BLE001
            if _code(e) in ("404", "NoSuchKey", "NotFound"):
                raise RemoteNotFound(f"{self.display_path(path)} does not exist") from e
            raise self._explain(e, "Downloading") from e
        try:
            yield from body.iter_chunks(CHUNK)
        finally:
            body.close()
