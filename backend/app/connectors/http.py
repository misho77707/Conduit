"""Files behind HTTP(S) URLs, via httpx. A saved HTTP connection is a base URL plus secret headers (an API token,
basic auth ...); a public file needs no connection at all (an ad-hoc URL).

Redirects are followed by hand (at most 5) so the secret headers are only ever sent to the connection's own
origin. URLs often carry tokens in the query string (pre-signed links), so anything shown to people goes through
mask_url(): scheme, host and path only.
"""
from __future__ import annotations

import email.utils
import posixpath
import re
from collections.abc import Iterator
from typing import Any, BinaryIO
from urllib.parse import unquote, urljoin, urlsplit

from .storage import CHUNK, RangeNotSupported, RangeReader, RemoteEntry, RemoteError, RemoteNotFound, StorageConnector

MAX_REDIRECTS = 5


def mask_url(url: str) -> str:
    """scheme://host[:port]/path: no credentials, no query string, no fragment."""
    try:
        u = urlsplit(url)
    except ValueError:
        return "(invalid URL)"
    if not u.scheme:
        return url.split("?", 1)[0].split("#", 1)[0]
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    return f"{u.scheme}://{host}{u.path}"


def mask_text(text: str, *urls: str) -> str:
    """Remove query strings of the given URLs (and of any other URL) from a message."""
    for url in urls:
        if url:
            text = text.replace(url, mask_url(url))
    return re.sub(r"(https?://[^\s?#'\"]+)\?[^\s'\"]*", r"\1", text)


def parse_headers(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _origin(url: str) -> tuple[str, str, int | None]:
    u = urlsplit(url)
    return u.scheme, (u.hostname or "").lower(), u.port


def _filename(resp, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;]+)", cd) or re.search(r'filename\s*=\s*"?([^";]+)"?', cd)
    if m:
        return posixpath.basename(unquote(m.group(1).strip()))
    return posixpath.basename(unquote(urlsplit(str(resp.url) if resp is not None else url).path)) or "download"


class HTTPConnector(StorageConnector):
    type = "http"
    label = "HTTP(S)"
    abbr = "WEB"
    kind = "http"
    default_port = None
    description = "Files behind URLs that need headers (an API token, basic auth). Public URLs need no connection."
    secret_fields = ("headers",)
    globbable = False

    @classmethod
    def validate(cls, config: dict[str, Any], secrets: dict[str, Any]) -> list[str]:
        base = str(config.get("base_url", "")).strip()
        if not base.startswith(("http://", "https://")):
            return ["Base URL must start with http:// or https://"]
        return []

    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or "").strip()

    def display_host(self) -> str:
        return mask_url(self.base_url)

    def url(self, path: str) -> str:
        """The full URL for `path`: relative to the base URL, or absolute (an ad-hoc URL, or one under the base)."""
        p = (path or "").strip()
        if p.startswith(("http://", "https://")):
            if self.base_url and not p.startswith(self.base_url.rstrip("/")):
                raise RemoteError(f"{mask_url(p)} is not under this connection's base URL {self.display_host()}")
            full = p
        elif not self.base_url:
            raise RemoteError("Give a full http:// or https:// URL")
        else:
            if any(s == ".." for s in p.split("?", 1)[0].split("/")):
                raise RemoteError("Paths may not contain '..': they are relative to the connection's base URL")
            full = self.base_url.rstrip("/") + "/" + p.lstrip("/") if p else self.base_url
        if not full.startswith(("http://", "https://")):
            raise RemoteError("Only http:// and https:// URLs are supported")
        return full

    def display_path(self, path: str) -> str:
        try:
            return mask_url(self.url(path))
        except RemoteError:
            return mask_url(path)

    # ---- requests (redirects by hand, secret headers only to the original origin) -------------
    def _client(self):
        import httpx

        return httpx.Client(follow_redirects=False, timeout=httpx.Timeout(max(self.timeout, 30), connect=self.timeout),
                            headers={"Accept-Encoding": "identity", "User-Agent": "Conduit"})

    def _send(self, client, method: str, url: str, extra: dict[str, str] | None = None):
        secret = parse_headers(self.secrets.get("headers"))
        origin = _origin(url)
        for _ in range(MAX_REDIRECTS + 1):
            headers = {**(secret if _origin(url) == origin else {}), **(extra or {})}
            try:
                resp = client.send(client.build_request(method, url, headers=headers), stream=True)
            except Exception as e:  # noqa: BLE001 - connect errors, timeouts, TLS
                raise RemoteError(mask_text(f"Could not reach {mask_url(url)}: {type(e).__name__}: {e}", url)) from e
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                resp.close()
                url = urljoin(url, resp.headers["location"])
                if method == "GET" or resp.status_code in (307, 308):
                    continue
                method = "GET"
                continue
            return resp
        raise RemoteError(f"{mask_url(url)}: more than {MAX_REDIRECTS} redirects")

    def _check(self, resp, url: str) -> None:
        if resp.status_code < 400:
            return
        resp.close()
        where = mask_url(url)
        if resp.status_code == 404:
            raise RemoteNotFound(f"{where} returned 404 Not Found")
        if resp.status_code in (401, 403):
            hint = "check the connection's headers" if self.secrets.get("headers") else \
                "the server wants credentials: use an HTTP connection with an Authorization header"
            raise RemoteError(f"{where} returned {resp.status_code} {resp.reason_phrase}: {hint}")
        raise RemoteError(f"{where} returned {resp.status_code} {resp.reason_phrase}")

    def _entry(self, path: str, resp, size: int | None) -> RemoteEntry:
        modified = None
        if lm := resp.headers.get("last-modified"):
            try:
                modified = email.utils.parsedate_to_datetime(lm)
            except (TypeError, ValueError):
                modified = None
        return RemoteEntry(name=_filename(resp, str(resp.url)), path=path, size=size, modified=modified,
                           etag=(resp.headers.get("etag") or "").strip('"') or None)

    def _probe(self, path: str) -> tuple[RemoteEntry, bool, str]:
        """(entry, server supports byte ranges, content type). HEAD first, then a 1-byte ranged GET."""
        url = self.url(path)
        with self._client() as client:
            resp = self._send(client, "HEAD", url)
            if resp.status_code in (403, 405, 501) or "content-length" not in resp.headers:
                resp.close()
                resp = self._send(client, "GET", url, {"Range": "bytes=0-0"})
                self._check(resp, url)
                resp.close()
                total = None
                if resp.status_code == 206 and (cr := resp.headers.get("content-range", "")) and "/" in cr:
                    t = cr.rsplit("/", 1)[1]
                    total = int(t) if t.isdigit() else None
                elif resp.headers.get("content-length", "").isdigit():
                    total = int(resp.headers["content-length"])
                return self._entry(path, resp, total), resp.status_code == 206, resp.headers.get("content-type", "")
            self._check(resp, url)
            resp.close()
            size = int(resp.headers["content-length"]) if resp.headers.get("content-length", "").isdigit() else None
            ranges = resp.headers.get("accept-ranges", "").lower() == "bytes"
            return self._entry(path, resp, size), ranges, resp.headers.get("content-type", "")

    # ---- test ----------------------------------------------------------------------------
    def _test(self) -> tuple[str, str]:
        try:
            e, _, ctype = self._probe("")
        except RemoteNotFound:
            return "HTTP", "reachable · the base URL itself returns 404 (fine when it is only a prefix for files)"
        size = f" · {e.size:,} bytes" if e.size is not None else ""
        return "HTTP", f"{ctype.split(';')[0] or 'reachable'}{size}"

    # ---- filesystem ----------------------------------------------------------------------
    def list(self, path: str) -> list[RemoteEntry]:
        raise RemoteError("HTTP sources are single files: browsing folders needs S3 or SFTP")

    def info(self, path: str) -> RemoteEntry:
        return self._probe(path)[0]

    def open(self, path: str) -> BinaryIO:
        entry, ranges, _ = self._probe(path)
        url = self.url(path)
        client = self._client()

        def read_range(start: int, length: int) -> bytes:
            if length <= 0:
                return b""
            resp = self._send(client, "GET", url, {"Range": f"bytes={start}-{start + length - 1}"})
            self._check(resp, url)
            try:
                if resp.status_code == 206:
                    return resp.read()
                if start != 0:  # the server ignored the range and is sending the whole file
                    raise RangeNotSupported(f"{mask_url(url)} does not support partial downloads")
                out = bytearray()
                for chunk in resp.iter_bytes(CHUNK):
                    out += chunk
                    if len(out) >= length:
                        break
                return bytes(out[:length])
            finally:
                resp.close()

        if entry.size is None:
            client.close()
            raise RangeNotSupported(f"{mask_url(url)} does not report its size")
        reader = RangeReader(entry.size, read_range, name=entry.name, on_close=client.close)
        reader.supports_ranges = ranges  # type: ignore[attr-defined]
        return reader  # type: ignore[return-value]

    def stream(self, path: str) -> Iterator[bytes]:
        url = self.url(path)
        with self._client() as client:
            resp = self._send(client, "GET", url)
            self._check(resp, url)
            try:
                yield from resp.iter_bytes(CHUNK)
            finally:
                resp.close()
