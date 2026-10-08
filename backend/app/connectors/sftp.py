"""SFTP servers via paramiko. Host keys are trusted on first use: the first successful connection learns the
server's SHA-256 fingerprint (stored in the connection's config); any later mismatch is a hard error."""
from __future__ import annotations

import base64
import hashlib
import io
import posixpath
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, BinaryIO

from .storage import CHUNK, RemoteEntry, RemoteError, RemoteNotFound, StorageConnector, clean_path

FP_KEY = "host_key_sha256"


class HostKeyChanged(RemoteError):
    pass


def fingerprint(key: Any) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def _load_key(text: str, passphrase: str | None):
    import paramiko

    last: Exception | None = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):  # the PEM / OpenSSH text says which one it is
        try:
            return cls.from_private_key(io.StringIO(text.strip() + "\n"), password=passphrase or None)
        except paramiko.PasswordRequiredException as e:
            raise RemoteError("The private key is encrypted: give its passphrase") from e
        except (paramiko.SSHException, ValueError) as e:
            last = e
    raise RemoteError(f"The private key could not be read (Ed25519, ECDSA or RSA in PEM / OpenSSH format): {last}")


class _SFTPFile(io.RawIOBase):
    """A remote file that keeps its SSH session open until closed."""

    def __init__(self, f, transport) -> None:
        self._f, self._t = f, transport
        self.name = getattr(f, "filename", "")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._f.seek(offset, whence)
        return self._f.tell()

    def tell(self) -> int:
        return self._f.tell()

    def readinto(self, b) -> int:
        data = self._f.read(len(b))
        b[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            try:
                self._f.close()
            finally:
                self._t.close()
        super().close()


class SFTPConnector(StorageConnector):
    type = "sftp"
    label = "SFTP"
    abbr = "SF"
    kind = "sftp"
    default_port = 22
    description = "Files on an SSH server, with a password or a private key."
    secret_fields = ("password", "private_key", "passphrase")

    @classmethod
    def validate(cls, config: dict[str, Any], secrets: dict[str, Any]) -> list[str]:
        errors = []
        if not str(config.get("host", "")).strip():
            errors.append("Host is required")
        if not str(config.get("user", "")).strip():
            errors.append("User is required")
        return errors

    @property
    def base(self) -> str:
        """The base folder: absolute ("/srv/exports") or relative to the user's home ("exports"); "" = home."""
        raw = str(self.config.get("base_path") or "").strip()
        rel = clean_path(raw)
        return "/" + rel if raw.startswith("/") else rel

    def _abs(self, path: str) -> str:
        p = clean_path(path)
        b = self.base
        return posixpath.join(b, p) if b and p else (b or p or ".")

    def _rel(self, abs_path: str) -> str:
        b = self.base
        return abs_path[len(b):].lstrip("/") if b and abs_path.startswith(b) else abs_path.lstrip("/")

    def display_host(self) -> str:
        port = int(self.config.get("port") or 22)
        return f"sftp://{self.config.get('user', '')}@{self.config.get('host', '')}:{port}/{clean_path(self.base)}".rstrip("/")

    # ---- connecting ------------------------------------------------------------------
    def _transport(self):
        import paramiko

        host, port = str(self.config.get("host", "")).strip(), int(self.config.get("port") or 22)
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as e:
            raise RemoteError(f"Could not reach {host}:{port}: {e}") from e
        t = paramiko.Transport(sock)
        t.banner_timeout = t.auth_timeout = self.timeout
        try:
            t.start_client(timeout=self.timeout)
            fp = fingerprint(t.get_remote_server_key())
            known = (self.config.get(FP_KEY) or "").strip()
            if known and known != fp:
                raise HostKeyChanged(f"The host key of {host} changed (now {fp}, stored {known}). If this is expected, "
                                     "clear the stored fingerprint in the connection's settings.")
            if not known:
                self.learned[FP_KEY] = fp  # trust on first use; the caller stores it
            user = str(self.config.get("user", ""))
            if self.secrets.get("private_key"):
                t.auth_publickey(user, _load_key(self.secrets["private_key"], self.secrets.get("passphrase")))
            elif self.secrets.get("password"):
                t.auth_password(user, self.secrets["password"])
            else:
                raise RemoteError("Give a password or a private key")
        except paramiko.AuthenticationException as e:
            t.close()
            raise RemoteError(f"Authentication failed for {user}@{host}: check the password / key") from e
        except paramiko.SSHException as e:
            t.close()
            raise RemoteError(f"SSH handshake with {host}:{port} failed: {e}") from e
        except BaseException:
            t.close()
            raise
        return t

    @contextmanager
    def _sftp(self):
        import paramiko

        t = self._transport()
        try:
            sftp = paramiko.SFTPClient.from_transport(t)
            try:  # the absolute base folder (resolves a base relative to the user's home), for RemoteEntry.full
                self._root_abs = sftp.normalize(self._abs(""))
            except OSError:
                self._root_abs = "/" + clean_path(self._abs(""))
            yield sftp
        finally:
            t.close()

    def _test(self) -> tuple[str, str]:
        with self._sftp() as sftp:
            try:
                n = len(sftp.listdir(self._abs("")))
            except OSError as e:
                raise RemoteError(f"Connected, but the base path {self.base or '(home)'} cannot be listed: {e}") from e
            version = sftp.get_channel().get_transport().remote_version
        return version.replace("SSH-2.0-", "") if version else "SFTP", f"{n} entr{'y' if n == 1 else 'ies'} in {self.base or 'the home folder'}"

    # ---- filesystem ------------------------------------------------------------------
    def _entry(self, rel: str, a) -> RemoteEntry:
        is_dir = stat.S_ISDIR(a.st_mode or 0)
        host, port = str(self.config.get("host", "")).strip(), int(self.config.get("port") or 22)
        root = getattr(self, "_root_abs", "") or "/"
        return RemoteEntry(
            name=posixpath.basename(rel), path=rel, is_dir=is_dir, size=None if is_dir else a.st_size,
            modified=datetime.fromtimestamp(a.st_mtime, tz=timezone.utc) if a.st_mtime else None,
            full=f"sftp://{host}:{port}{posixpath.join(root, rel) if rel else root}")

    def list(self, path: str) -> list[RemoteEntry]:
        rel = clean_path(path)
        with self._sftp() as sftp:
            try:
                attrs = sftp.listdir_attr(self._abs(rel))
            except FileNotFoundError as e:
                raise RemoteNotFound(f"{self.display_path(rel)} does not exist") from e
            except OSError as e:
                raise RemoteError(f"Cannot list {self.display_path(rel)}: {e}") from e
        out = [self._entry(posixpath.join(rel, a.filename) if rel else a.filename, a) for a in attrs if not a.filename.startswith(".")]
        return sorted(out, key=lambda e: (not e.is_dir, e.name.lower()))

    def walk(self, folder: str, recursive: bool, limit: int) -> Iterator[RemoteEntry]:
        with self._sftp() as sftp:  # one session for the whole walk
            seen, stack = 0, [clean_path(folder)]
            while stack:
                rel = stack.pop()
                try:
                    attrs = sftp.listdir_attr(self._abs(rel))
                except FileNotFoundError:
                    continue
                for a in attrs:
                    if a.filename.startswith("."):
                        continue
                    seen += 1
                    if seen > limit:
                        raise RemoteError(f"More than {limit:,} entries under '{folder or '/'}': narrow the pattern's folder")
                    e = self._entry(posixpath.join(rel, a.filename) if rel else a.filename, a)
                    if e.is_dir:
                        if recursive:
                            stack.append(e.path)
                    else:
                        yield e

    def info(self, path: str) -> RemoteEntry:
        rel = clean_path(path)
        with self._sftp() as sftp:
            try:
                return self._entry(rel, sftp.stat(self._abs(rel)))
            except FileNotFoundError as e:
                raise RemoteNotFound(f"{self.display_path(rel)} does not exist") from e

    def open(self, path: str) -> BinaryIO:
        import paramiko

        t = self._transport()
        try:
            f = paramiko.SFTPClient.from_transport(t).open(self._abs(path), "rb")
        except FileNotFoundError as e:
            t.close()
            raise RemoteNotFound(f"{self.display_path(path)} does not exist") from e
        except BaseException:
            t.close()
            raise
        return _SFTPFile(f, t)  # type: ignore[return-value]

    def stream(self, path: str) -> Iterator[bytes]:
        with self.open(path) as f:
            f._f.prefetch()  # pipelined reads: much faster than one round trip per chunk
            while chunk := f.read(CHUNK):
                yield chunk
