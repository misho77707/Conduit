"""A tiny in-process SFTP server (paramiko's server API) over a local folder, for tests: password or public-key
auth, read-only file access, listing and stat. Each SFTPStub has its own host key, so starting a second one on
the same port simulates a server whose host key changed."""
from __future__ import annotations

import os
import posixpath
import socket
import threading
from pathlib import Path

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface, ServerInterface
from paramiko.sftp import SFTP_NO_SUCH_FILE, SFTP_OK, SFTP_PERMISSION_DENIED


class _Auth(ServerInterface):
    def __init__(self, stub: "SFTPStub") -> None:
        self.stub = stub

    def check_auth_password(self, username, password):
        ok = username == self.stub.user and password == self.stub.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        ok = username == self.stub.user and self.stub.client_key is not None and key.asbytes() == self.stub.client_key.asbytes()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class _Handle(SFTPHandle):
    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


def _make_sftp(root: Path):
    class _SFTP(SFTPServerInterface):
        def _real(self, path: str) -> str:
            return str(root / self.canonicalize(path).lstrip("/"))

        def canonicalize(self, path: str) -> str:
            return posixpath.normpath("/" + path) if path not in ("", ".") else "/"

        def list_folder(self, path):
            real = self._real(path)
            if not os.path.isdir(real):
                return SFTP_NO_SUCH_FILE
            out = []
            for name in os.listdir(real):
                a = SFTPAttributes.from_stat(os.stat(os.path.join(real, name)))
                a.filename = name
                out.append(a)
            return out

        def stat(self, path):
            real = self._real(path)
            return SFTPAttributes.from_stat(os.stat(real)) if os.path.exists(real) else SFTP_NO_SUCH_FILE

        lstat = stat

        def open(self, path, flags, attr):
            if flags & (os.O_WRONLY | os.O_RDWR):
                return SFTP_PERMISSION_DENIED
            real = self._real(path)
            if not os.path.isfile(real):
                return SFTP_NO_SUCH_FILE
            h = _Handle(flags)
            h.filename = real
            h.readfile = open(real, "rb")  # noqa: SIM115 - closed by paramiko with the handle
            return h

        def remove(self, path):
            return SFTP_PERMISSION_DENIED

        def session_ended(self):
            return SFTP_OK

    return _SFTP


class SFTPStub:
    def __init__(self, root: Path, user: str = "conduit", password: str = "secret", port: int = 0) -> None:
        self.root, self.user, self.password = root, user, password
        self.host_key = paramiko.ECDSAKey.generate()  # fast to generate; a new one per stub = "host key changed"
        self.client_key: paramiko.PKey | None = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._transports: list[paramiko.Transport] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        handler = _make_sftp(self.root)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", SFTPServer, handler)
            self._transports.append(t)
            try:
                t.start_server(server=_Auth(self))
            except Exception:  # noqa: BLE001 - a client that gave up during the handshake
                continue

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the accept() thread so the port is really released
        except OSError:
            pass
        self.sock.close()
        for t in self._transports:
            t.close()
