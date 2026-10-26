"""Export / import (Control & Setup): the instance's configuration as one JSON file, references by name.

The file holds connections, service checks, pipelines (steps, schedule, enabled), alert channels and rules, and the
runtime settings saved in the UI. Never users, jobs, comparisons, watermarks, loaded-file records, alert state or
.env values. Secrets stay out unless asked for: then each object carries a Fernet token under a key derived from a
passphrase (scrypt), never the instance key, and a `check` token tells a wrong passphrase apart before anything.

Import is a plan (a dry run: create / update / skip / conflict per object, with notes), then an apply of the picked
objects, each committed on its own so one failure does not stop the rest. Imported pipelines arrive disabled with
no cached preview; a connection without its secrets is marked needs_credentials; a channel without its secrets
arrives disabled.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import runtime
from .alerts import senders
from .alerts.catalog import EVENTS, normalize_options, normalize_scope
from .connectors import REGISTRY, category_of, get_connector
from .engine.runner import short_error
from .models import AlertChannel, AlertRule, AlertState, Connection, Pipeline, ServiceCheck, Setting, User, utcnow
from .security import SecretKeyError, decrypt_secrets, encrypt_secrets
from .services import NEEDS_NOTE, set_secrets

FORMAT, VERSION = "conduit-export", 1
SECTIONS = ("settings", "connections", "service_checks", "pipelines", "alert_channels", "alert_rules")  # apply order
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSPHRASE = 12
_CHECK = b"conduit-export"
_NO_SQL = ("mongodb",)  # query checks need a SQL database (as in api/monitor.py)


class TransferError(ValueError):
    """A file this Conduit cannot read, or a wrong passphrase: nothing is imported (422)."""


class ItemError(Exception):
    """One picked object could not be applied; the others still are."""


# ---- passphrase encryption -----------------------------------------------------------------------------------

def _fernet(passphrase: str, salt: bytes, n: int, r: int, p: int) -> Fernet:
    if not (isinstance(n, int) and 2 <= n <= 2 ** 20 and n & (n - 1) == 0 and isinstance(r, int) and 1 <= r <= 32
            and isinstance(p, int) and 1 <= p <= 4):
        raise TransferError("The file's encryption settings are not supported")
    try:
        raw = hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)
    except (ValueError, MemoryError) as e:
        raise TransferError("The file's encryption settings are not supported") from e
    return Fernet(base64.urlsafe_b64encode(raw))


def _opener(doc: dict, passphrase: str | None) -> Fernet | None:
    """The key that opens the file's secrets, None when there are none (or no passphrase was given)."""
    if doc.get("secrets") != "encrypted" or not passphrase:
        return None
    enc = doc.get("encryption")
    if not isinstance(enc, dict) or enc.get("kdf") != "scrypt":
        raise TransferError("The file's encryption block is missing or not scrypt")
    try:
        salt = base64.b64decode(str(enc.get("salt") or ""), validate=True)
    except ValueError:
        raise TransferError("The file's encryption salt is damaged") from None
    f = _fernet(passphrase, salt, enc.get("n"), enc.get("r"), enc.get("p"))
    try:
        if f.decrypt(str(enc.get("check") or "").encode()) != _CHECK:
            raise InvalidToken
    except InvalidToken:
        raise TransferError("Wrong passphrase for this file") from None
    return f


def _stored(enc: str) -> dict[str, Any]:
    if not enc:
        return {}
    try:
        return decrypt_secrets(enc)
    except SecretKeyError:
        return {}


# ---- references by name ----------------------------------------------------------------------------------------

def _by_name(obj: Any, names: dict[str, str]) -> Any:
    """connection_id anywhere in a pipeline's source / target -> "connection": its name."""
    if isinstance(obj, dict):
        return {("connection" if k == "connection_id" else k): (names.get(str(v)) if v else None) if k == "connection_id"
                else _by_name(v, names) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_by_name(x, names) for x in obj]
    return obj


def _by_id(obj: Any, ids: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {("connection_id" if k == "connection" else k): (ids[v] if v else None) if k == "connection"
                else _by_id(v, ids) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_by_id(x, ids) for x in obj]
    return obj


def _refs(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        out = {str(obj["connection"])} if obj.get("connection") else set()
        for k, v in obj.items():
            if k != "connection":
                out |= _refs(v)
        return out
    if isinstance(obj, list):
        return set().union(*(_refs(x) for x in obj)) if obj else set()
    return set()


def _secret_label(name: str) -> str:
    return {"url": "URL", "webhook_url": "webhook URL", "uri": "connection string"}.get(name, name.replace("_", " "))


def _listing(names: list[str]) -> str:
    names = [str(n) for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


_FIELD = {"env": "environment tag", "config": "settings", "enabled": "on / off", "interval_s": "interval",
          "verify_tls": "TLS check", "expect": "expected value"}


def _changes(cur: dict, new: dict) -> str:
    """An update's note: which parts the file changes."""
    return "Changes its " + _listing([_FIELD.get(k, k) for k in new if cur.get(k) != new.get(k)])


def _z(dt) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- export ------------------------------------------------------------------------------------------------------

def export(session: Session, passphrase: str | None = None) -> dict[str, Any]:
    f = None
    doc: dict[str, Any] = {"format": FORMAT, "version": VERSION, "instance": runtime.current_settings().instance_name,
                           "exported_at": _z(utcnow()), "secrets": "encrypted" if passphrase is not None else "none"}
    if passphrase is not None:
        if len(passphrase) < MIN_PASSPHRASE:
            raise TransferError(f"The passphrase needs at least {MIN_PASSPHRASE} characters")
        salt = os.urandom(16)
        f = _fernet(passphrase, salt, **SCRYPT)
        doc["encryption"] = {"kdf": "scrypt", **SCRYPT, "salt": base64.b64encode(salt).decode(),
                             "check": f.encrypt(_CHECK).decode()}

    def secret_part(enc: str) -> dict[str, Any]:
        s = _stored(enc)
        return {"secrets_set": sorted(s), **({"secrets": f.encrypt(json.dumps(s).encode()).decode()} if f and s else {})}

    conns = list(session.scalars(select(Connection).order_by(Connection.name)))
    checks = list(session.scalars(select(ServiceCheck).order_by(ServiceCheck.name)))
    pipes = list(session.scalars(select(Pipeline).order_by(Pipeline.name)))
    cnames = {str(c.id): c.name for c in conns}
    names = {"connections": cnames, "pipelines": {str(p.id): p.name for p in pipes},
             "checks": {str(c.id): c.name for c in checks}}
    doc["settings"] = {r.key: r.value for r in session.scalars(
        select(Setting).where(Setting.key.in_(runtime.RUNTIME)).order_by(Setting.key))}
    doc["connections"] = [{"name": c.name, "type": c.type, "env": c.env, "config": c.config or {}, **secret_part(c.secret_enc),
                           **({"needs_credentials": True} if c.needs_credentials else {})} for c in conns]
    doc["service_checks"] = [{"name": c.name, "kind": c.kind, "target": c.target,
                              "connection": cnames.get(str(c.connection_id)) if c.connection_id else None,
                              "query": c.query, "expect": c.expect, "interval_s": c.interval_s, "verify_tls": c.verify_tls,
                              "enabled": c.enabled} for c in checks]
    doc["pipelines"] = [{"name": p.name, "description": p.description, "enabled": p.enabled, "schedule": p.schedule,
                         "source": _by_name(p.source or {}, cnames), "target": _by_name(p.target or {}, cnames),
                         "options": p.options or {}, "steps": p.steps or []} for p in pipes]
    doc["alert_channels"] = [{"name": c.name, "kind": c.kind, "enabled": c.enabled, "config": c.config or {},
                              **secret_part(c.secret_enc)}
                             for c in session.scalars(select(AlertChannel).order_by(AlertChannel.name))]
    doc["alert_rules"] = []
    for r in session.scalars(select(AlertRule).order_by(AlertRule.name)):
        scope = r.scope or {"all": True}
        out_scope = {"all": True} if scope.get("all") else {
            k: sorted(names[k].get(str(x), "") for x in v if names[k].get(str(x))) if k in names else v for k, v in scope.items()}
        doc["alert_rules"].append({"name": r.name, "event": r.event, "enabled": r.enabled, "options": r.options or {},
                                   "scope": out_scope, "channels": sorted(c.name for c in r.channels)})
    return doc


# ---- plan --------------------------------------------------------------------------------------------------------

@dataclass
class Item:
    key: str  # section/index (settings: section/key): what the apply picks
    section: str
    name: str
    action: str  # create | update | skip | conflict
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)  # what apply writes

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "section": self.section, "name": self.name, "action": self.action, "notes": self.notes}


@dataclass
class Plan:
    instance: str
    exported_at: str | None
    version: int
    secrets: str
    secrets_used: bool
    notes: list[str]
    items: list[Item]

    def public(self) -> dict[str, Any]:
        return {"instance": self.instance, "exported_at": self.exported_at, "version": self.version,
                "secrets": self.secrets, "secrets_used": self.secrets_used, "notes": self.notes,
                "items": [i.public() for i in self.items]}


class _Ctx:
    def __init__(self, session: Session, doc: dict, f: Fernet | None):
        self.session, self.doc, self.f = session, doc, f
        self.ok: dict[str, dict[str, Any]] = {s: {} for s in SECTIONS}  # applicable file objects, by name
        self.conns = {c.name: c for c in session.scalars(select(Connection))}
        self.pipes = {p.name: p for p in session.scalars(select(Pipeline))}
        self.checks: dict[str, list[ServiceCheck]] = {}
        for c in session.scalars(select(ServiceCheck)):
            self.checks.setdefault(c.name, []).append(c)
        self.chans = {c.name: c for c in session.scalars(select(AlertChannel))}
        self.rules = {r.name: r for r in session.scalars(select(AlertRule))}

    def open(self, o: dict) -> dict[str, Any] | None:
        token = o.get("secrets")
        if not self.f or not token:
            return None
        try:
            s = json.loads(self.f.decrypt(str(token).encode()))
        except (InvalidToken, ValueError):
            raise TransferError(f"The secrets of {o.get('name')!r} are damaged") from None
        return s if isinstance(s, dict) else None

    def sql_connection(self, name: str) -> bool:
        """A connection a query check can use: here, or applicable in the file."""
        c = self.conns.get(name)
        t = c.type if c is not None else (self.ok["connections"].get(name) or {}).get("type")
        return bool(t) and category_of(t) == "database" and t not in _NO_SQL

    def connection(self, name: str) -> bool:
        return name in self.conns or name in self.ok["connections"]


def _placeholder(field_name: str) -> Any:
    """Stands in for a secret the file left out, so the rest of an object can still be checked."""
    return {"uri": "mongodb://imported", "headers": None, "url": "https://imported.invalid/",
            "webhook_url": "https://imported.invalid/"}.get(field_name, "x")


def _trial(secret_names: list[str], secrets: dict | None) -> dict[str, Any]:
    out = {k: _placeholder(k) for k in secret_names}
    return {k: v for k, v in {**out, **(secrets or {})}.items() if v is not None}


def _objects(doc: dict, section: str) -> list[tuple[int, dict]]:
    raw = doc.get(section) or []
    if not isinstance(raw, list):
        raise TransferError(f"{section} in the file is not a list")
    return [(i, o) for i, o in enumerate(raw) if isinstance(o, dict)]


def _named(section: str, objs: list[tuple[int, dict]], plan_one: Callable[[str, dict], Item]) -> list[Item]:
    seen: set[str] = set()
    out = []
    for i, o in objs:
        name = str(o.get("name") or "").strip()
        key = f"{section}/{i}"
        if not name:
            out.append(Item(key, section, f"#{i + 1}", "conflict", ["It has no name"]))
            continue
        if name in seen:
            out.append(Item(key, section, name, "conflict", ["This name appears twice in the file: the first one is used"]))
            continue
        seen.add(name)
        it = plan_one(key, {**o, "name": name})
        out.append(it)
    return out


def _plan_settings(ctx: _Ctx) -> list[Item]:
    raw = ctx.doc.get("settings") or {}
    if not isinstance(raw, dict):
        raise TransferError("settings in the file is not an object")
    rows = {r.key: r for r in ctx.session.scalars(select(Setting).where(Setting.key.in_(runtime.RUNTIME)))}
    now = runtime.current_settings()
    items = []
    for key, value in raw.items():
        spec = runtime.SPECS.get(key)
        k = f"settings/{key}"
        if key not in runtime.RUNTIME or spec is None:
            items.append(Item(k, "settings", key, "conflict", ["Not a setting this Conduit lets you change"]))
            continue
        try:
            v = runtime.check(key, value)
        except runtime.SettingError as e:
            items.append(Item(k, "settings", spec.label, "conflict", [f"{e} on this Conduit"]))
            continue
        row = rows.get(key)
        if row is not None and row.value == v:
            items.append(Item(k, "settings", spec.label, "skip", ["The same here"]))
            continue
        unit = f" {spec.unit}" if spec.unit else ""
        items.append(Item(k, "settings", spec.label, "update" if row is not None else "create",
                          [f"{getattr(now, key)!r}{unit} here → {v!r}{unit}"], {"key": key, "value": v}))
    return items


def _plan_connection(ctx: _Ctx, key: str, o: dict) -> Item:
    name, type_ = o["name"], o.get("type")
    it = Item(key, "connections", name, "conflict")
    if type_ not in REGISTRY:
        it.notes.append(f"Unknown connection type {type_!r}")
        return it
    config = o.get("config") or {}
    if not isinstance(config, dict):
        it.notes.append("Its config is not an object")
        return it
    secrets = ctx.open(o)
    secret_names = [str(x) for x in o.get("secrets_set") or []] or sorted(secrets or {})
    errors = REGISTRY[type_].validate(config, _trial(secret_names or list(REGISTRY[type_].secret_fields), secrets))
    if errors:
        it.notes.append("; ".join(errors))
        return it
    have = ctx.conns.get(name)
    if have is not None and have.type != type_:
        it.notes.append(f"Exists here as a {have.type} connection; the file has {type_}")
        return it
    needs = (bool(secret_names) or bool(o.get("needs_credentials"))) and not secrets
    env = (o.get("env") or None)
    if have is None:
        it.action = "create"
        if needs:
            it.notes.append(f"Needs its {_listing([_secret_label(s) for s in secret_names]) or 'secrets'}: unusable until you edit it")
        elif secrets:
            it.notes.append("Secrets included")
    else:
        cur, new = {"env": have.env or None, "config": have.config or {}}, {"env": env, "config": config}
        if secrets is not None:
            cur["secrets"], new["secrets"] = _stored(have.secret_enc), secrets
        it.action = "skip" if cur == new else "update"
        if cur == new:
            it.notes.append("The same here")
        else:
            it.notes.append(_changes(cur, new))
            if secrets is None and have.secret_enc:
                it.notes.append("Keeps the secrets saved here")
        if have.needs_credentials and not secrets:
            it.notes.append("Still needs credentials here")
    it.data = {"type": type_, "env": env, "config": config, "secrets": secrets, "needs": needs}
    ctx.ok["connections"][name] = it.data
    return it


def _plan_check(ctx: _Ctx, key: str, o: dict) -> Item:
    from .api.monitor import CheckIn

    name = o["name"]
    it = Item(key, "service_checks", name, "conflict")
    conn = o.get("connection") if o.get("kind") == "query" else None
    try:
        body = CheckIn(name=name, kind=o.get("kind"), target=o.get("target") or "", query=o.get("query"),
                       connection_id=uuid.uuid4() if conn else None, expect=str(o.get("expect") or ""),
                       interval_s=o.get("interval_s") or 30, verify_tls=o.get("verify_tls", True), enabled=o.get("enabled", True))
    except ValidationError as e:
        it.notes.append(short_error(e))
        return it
    if body.kind == "query" and not conn:
        it.notes.append("A query check without its connection")
        return it
    if conn and not ctx.sql_connection(conn):
        it.notes.append(f"Missing reference: SQL connection {conn!r} (not here, not in the file)")
        return it
    here = ctx.checks.get(name, [])
    if len(here) > 1:
        it.notes.append(f"{len(here)} service checks here are called {name!r}: rename one first")
        return it
    fields = {"kind": body.kind, "target": body.target, "connection": conn, "query": body.query if conn else None,
              "expect": body.expect, "interval_s": body.interval_s, "verify_tls": body.verify_tls, "enabled": body.enabled}
    if here:
        h = here[0]
        if h.kind != body.kind:
            it.notes.append(f"Exists here as a {h.kind} check; the file has {body.kind}")
            return it
        cur = {"kind": h.kind, "target": h.target, "connection": next((c.name for c in ctx.conns.values() if c.id == h.connection_id), None),
               "query": h.query, "expect": h.expect, "interval_s": h.interval_s, "verify_tls": h.verify_tls, "enabled": h.enabled}
        it.action = "skip" if cur == fields else "update"
        it.notes.append("The same here" if it.action == "skip" else _changes(cur, fields))
    else:
        it.action = "create"
    it.data = fields
    ctx.ok["service_checks"][name] = fields
    return it


def _plan_pipeline(ctx: _Ctx, key: str, o: dict) -> Item:
    from .api.pipelines import _check_schedule, _validate_shape

    name = o["name"]
    it = Item(key, "pipelines", name, "conflict")
    source, target = o.get("source") or {}, o.get("target") or {}
    options, steps, schedule = o.get("options") or {}, o.get("steps") or [], o.get("schedule") or None
    if not isinstance(source, dict) or not isinstance(target, dict) or not isinstance(options, dict) or not isinstance(steps, list):
        it.notes.append("Its source, target, options or steps are damaged")
        return it
    missing = sorted(n for n in _refs(source) | _refs(target) if not ctx.connection(n))
    if missing:
        it.notes.append("Missing reference: " + _listing([f"connection {n!r}" for n in missing]) + " (not here, not in the file)")
        return it
    fake = {n: str(uuid.uuid5(uuid.NAMESPACE_URL, n)) for n in _refs(source) | _refs(target)}
    try:
        _validate_shape(_by_id(source, fake), _by_id(target, fake), options, steps)
        _check_schedule(schedule)
    except HTTPException as e:
        it.notes.append(str(e.detail))
        return it
    fields = {"description": str(o.get("description") or "")[:500], "source": source, "target": target,
              "options": options, "steps": steps, "schedule": schedule}
    have = ctx.pipes.get(name)
    if have is not None:
        cnames = {str(c.id): c.name for c in ctx.conns.values()}
        cur = {"description": have.description or "", "source": _by_name(have.source or {}, cnames),
               "target": _by_name(have.target or {}, cnames), "options": have.options or {}, "steps": have.steps or [],
               "schedule": have.schedule}
        if cur == fields:
            it.action = "skip"
            it.notes.append("The same here")
            it.data = fields
            ctx.ok["pipelines"][name] = fields
            return it
        it.action = "update"
        it.notes.append(_changes(cur, fields))
    else:
        it.action = "create"
    it.notes.append(f"Arrives disabled{f', schedule kept ({schedule})' if schedule else ''}: refresh its preview, then enable it")
    remote = source.get("remote") or {}
    if remote.get("match") in ("newest", "all") and remote.get("skip_loaded", True):
        it.notes.append("Files already loaded elsewhere are not known here: its first run loads every matching file")
    if (source.get("combined") or {}).get("watermark"):
        it.notes.append("Its watermark starts empty: the first run reads the driver input whole")
    it.data = fields
    ctx.ok["pipelines"][name] = fields
    return it


def _plan_channel(ctx: _Ctx, key: str, o: dict) -> Item:
    name, kind = o["name"], o.get("kind")
    it = Item(key, "alert_channels", name, "conflict")
    if kind not in senders.KINDS:
        it.notes.append(f"Unknown channel kind {kind!r}")
        return it
    secrets = ctx.open(o)
    secret_names = [str(x) for x in o.get("secrets_set") or []]
    try:
        config, _ = senders.check_channel(kind, o.get("config") or {}, _trial(secret_names or list(senders.SECRET_FIELDS[kind]), secrets))
    except ValueError as e:
        it.notes.append(str(e))
        return it
    enabled = bool(o.get("enabled", True))
    have = ctx.chans.get(name)
    if have is not None and have.kind != kind:
        it.notes.append(f"Exists here as a {have.kind} channel; the file has {kind}")
        return it
    needs = bool(secret_names) and not secrets and (have is None or not have.secret_enc)
    if have is None:
        it.action = "create"
    else:
        cur, new = {"config": have.config or {}, "enabled": have.enabled}, {"config": config, "enabled": enabled and not needs}
        if secrets is not None:
            cur["secrets"], new["secrets"] = _stored(have.secret_enc), secrets
        it.action = "skip" if cur == new else "update"
        if cur == new:
            it.notes.append("The same here")
        else:
            it.notes.append(_changes(cur, new))
            if secrets is None and have.secret_enc:
                it.notes.append("Keeps the secrets saved here")
    if needs:
        it.notes.append(f"Needs its {_listing([_secret_label(s) for s in secret_names])}: arrives disabled until you edit it")
    it.data = {"kind": kind, "config": config, "secrets": secrets, "enabled": enabled and not needs, "needs": needs,
               "secret_names": secret_names}
    ctx.ok["alert_channels"][name] = it.data
    return it


_SCOPE_KIND = {"pipelines": "pipeline", "connections": "connection", "checks": "service check"}


def _plan_rule(ctx: _Ctx, key: str, o: dict) -> Item:
    name, event = o["name"], o.get("event")
    it = Item(key, "alert_rules", name, "conflict")
    if event not in EVENTS:
        it.notes.append(f"Unknown event {event!r}")
        return it
    try:
        options = normalize_options(event, o.get("options") or {})
    except ValueError as e:
        it.notes.append(str(e))
        return it
    raw = o.get("scope") or {"all": True}
    if not isinstance(raw, dict):
        it.notes.append("Its scope is damaged")
        return it
    scope: dict[str, Any] = {"all": True}
    if not raw.get("all"):
        exists = {"pipelines": lambda n: n in ctx.pipes or n in ctx.ok["pipelines"],
                  "connections": ctx.connection, "checks": lambda n: n in ctx.checks or n in ctx.ok["service_checks"]}
        scope, left_out = {}, []
        for k in EVENTS[event].scope_keys:
            names = [str(x) for x in raw.get(k) or []]
            if k in exists:
                left_out += [f"{_SCOPE_KIND[k]} {n!r}" for n in names if not exists[k](n)]
                names = [n for n in names if exists[k](n)]
            if names:
                scope[k] = sorted(set(names))
        if left_out:
            it.notes.append("Left out of its scope (not here, not in the file): " + _listing(left_out))
        if not scope:
            it.notes.append("Nothing it watches exists here or in the file")
            return it
    chans = [str(c) for c in o.get("channels") or []]
    gone = [c for c in chans if c not in ctx.chans and c not in ctx.ok["alert_channels"]]
    if gone:
        it.notes.append("Sends nowhere for " + _listing([repr(c) for c in gone]) + " (not here, not in the file)")
    chans = sorted(set(c for c in chans if c not in gone))
    fields = {"event": event, "options": options, "scope": scope, "channels": chans, "enabled": bool(o.get("enabled", True))}
    have = ctx.rules.get(name)
    if have is not None:
        names = {"pipelines": {str(p.id): p.name for p in ctx.pipes.values()},
                 "connections": {str(c.id): c.name for c in ctx.conns.values()},
                 "checks": {str(c.id): n for n, cs in ctx.checks.items() for c in cs}}
        hs = have.scope or {"all": True}
        cur_scope = {"all": True} if hs.get("all") else {
            k: sorted(names[k].get(str(x), str(x)) for x in v) if k in names else sorted(v) for k, v in hs.items()}
        cur = {"event": have.event, "options": have.options or {}, "scope": cur_scope,
               "channels": sorted(c.name for c in have.channels), "enabled": have.enabled}
        it.action = "skip" if cur == fields else "update"
        it.notes.append("The same here" if it.action == "skip" else _changes(cur, fields))
    else:
        it.action = "create"
    it.data = fields
    ctx.ok["alert_rules"][name] = fields
    return it


def plan(session: Session, doc: Any, passphrase: str | None = None) -> Plan:
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise TransferError("Not a Conduit export file")
    version = doc.get("version")
    if not isinstance(version, int) or version < 1:
        raise TransferError("The file has no format version")
    if version > VERSION:
        raise TransferError(f"This file was made by a newer Conduit (format {version}); this one reads format {VERSION}. "
                            "Upgrade this Conduit first.")
    f = _opener(doc, passphrase)
    ctx = _Ctx(session, doc, f)
    items = _plan_settings(ctx)
    items += _named("connections", _objects(doc, "connections"), lambda k, o: _plan_connection(ctx, k, o))
    items += _named("service_checks", _objects(doc, "service_checks"), lambda k, o: _plan_check(ctx, k, o))
    items += _named("pipelines", _objects(doc, "pipelines"), lambda k, o: _plan_pipeline(ctx, k, o))
    items += _named("alert_channels", _objects(doc, "alert_channels"), lambda k, o: _plan_channel(ctx, k, o))
    items += _named("alert_rules", _objects(doc, "alert_rules"), lambda k, o: _plan_rule(ctx, k, o))
    notes = ["Watermarks, loaded-file records, jobs, comparisons, users and alert state are never imported."]
    if doc.get("secrets") == "encrypted" and f is None:
        notes.append("The file has encrypted secrets, but no passphrase was given: they are left out.")
    elif doc.get("secrets") != "encrypted":
        notes.append("The file has no secrets: new connections and channels that need them arrive unusable until edited.")
    return Plan(instance=str(doc.get("instance") or "?"), exported_at=doc.get("exported_at"), version=version,
                secrets=str(doc.get("secrets") or "none"), secrets_used=f is not None, notes=notes, items=items)


# ---- apply -------------------------------------------------------------------------------------------------------

def _apply_setting(s: Session, user: User, it: Item) -> None:
    row = s.get(Setting, it.data["key"])
    if row is None:
        row = Setting(key=it.data["key"])
        s.add(row)
    row.value, row.updated_by_id, row.updated_at = it.data["value"], user.id, utcnow()


def _apply_connection(s: Session, user: User, it: Item) -> None:
    d = it.data
    c = s.scalar(select(Connection).where(Connection.name == it.name))
    if c is None:
        c = Connection(name=it.name, type=d["type"], owner_id=user.id)
        s.add(c)
        set_secrets(c, {})
        c.needs_credentials = d["needs"]
    c.env, c.config = d["env"], d["config"]
    if d["secrets"] is not None:
        set_secrets(c, d["secrets"])
        c.needs_credentials = False
    c.display_host = get_connector(c.type, c.config, d["secrets"] or _stored(c.secret_enc)).display_host()
    c.status, c.note = "idle", NEEDS_NOTE if c.needs_credentials else "Imported: not checked yet"
    c.err_streak, c.err_since, c.bad_streak, c.bad_since = 0, None, 0, None


def _apply_check(s: Session, user: User, it: Item) -> None:
    d = it.data
    conn_id = None
    if d["connection"]:
        conn = s.scalar(select(Connection).where(Connection.name == d["connection"]))
        if conn is None:
            raise ItemError(f"Connection {d['connection']!r} is not here: pick it too")
        conn_id = conn.id
    c = s.scalar(select(ServiceCheck).where(ServiceCheck.name == it.name))
    if c is None:
        c = ServiceCheck(name=it.name, kind=d["kind"], owner_id=user.id)
        s.add(c)
    c.target, c.connection_id, c.query, c.expect = d["target"], conn_id, d["query"], d["expect"]
    c.interval_s, c.verify_tls, c.enabled = d["interval_s"], d["verify_tls"], d["enabled"]
    c.status, c.message, c.latency_ms, c.checked_at = "pending", "waiting for first check", None, None
    c.fail_streak, c.fail_since = 0, None


def _apply_pipeline(s: Session, user: User, it: Item) -> None:
    from .api.pipelines import _check_connections, _validate_shape

    d = it.data
    refs = _refs(d["source"]) | _refs(d["target"])
    ids = {c.name: str(c.id) for c in s.scalars(select(Connection).where(Connection.name.in_(refs)))} if refs else {}
    missing = sorted(refs - ids.keys())
    if missing:
        raise ItemError(_listing([f"Connection {n!r}" for n in missing]) + " is not here: pick it too")
    source, target = _by_id(d["source"], ids), _by_id(d["target"], ids)
    try:
        _validate_shape(source, target, d["options"], d["steps"])
        _check_connections(s, source, target)
    except HTTPException as e:
        raise ItemError(str(e.detail)) from None
    p = s.scalar(select(Pipeline).where(Pipeline.name == it.name))
    if p is None:
        p = Pipeline(name=it.name, owner_id=user.id)
        s.add(p)
    p.description, p.source, p.target, p.options, p.steps = d["description"], source, target, d["options"], d["steps"]
    p.schedule, p.enabled, p.next_run_at, p.schedule_claimed_at = d["schedule"], False, None, None
    p.output_columns = p.source_columns = None  # no cached preview: /run and the scheduler wait for a fresh one


def _apply_channel(s: Session, user: User, it: Item) -> None:
    d = it.data
    ch = s.scalar(select(AlertChannel).where(AlertChannel.name == it.name))
    if ch is None:
        ch = AlertChannel(name=it.name, kind=d["kind"], secret_enc="")
        s.add(ch)
    ch.config, ch.enabled = d["config"], d["enabled"]
    if d["secrets"] is not None:
        _, secrets = senders.check_channel(d["kind"], d["config"], d["secrets"])
        ch.secret_enc, ch.last_error = encrypt_secrets(secrets), None
    elif d["needs"]:
        ch.last_error = (f"Needs its {_listing([_secret_label(n) for n in d['secret_names']])}: imported without "
                         "secrets. Edit the channel.")


def _apply_rule(s: Session, user: User, it: Item) -> None:
    d = it.data
    scope: dict[str, Any] = {"all": True}
    if not d["scope"].get("all"):
        lookup = {"pipelines": (Pipeline, Pipeline.name), "connections": (Connection, Connection.name),
                  "checks": (ServiceCheck, ServiceCheck.name)}
        scope = {}
        for k, names in d["scope"].items():
            if k in lookup:
                model, col = lookup[k]
                names = [str(x.id) for x in s.scalars(select(model).where(col.in_(names)))]
            if names:
                scope[k] = names
        if not scope:
            raise ItemError("Nothing it watches is here: pick those objects too")
    chans = list(s.scalars(select(AlertChannel).where(AlertChannel.name.in_(d["channels"])))) if d["channels"] else []
    r = s.scalar(select(AlertRule).where(AlertRule.name == it.name))
    if r is None:
        r = AlertRule(name=it.name)
        s.add(r)
    elif r.event != d["event"] or not d["enabled"]:  # its current state no longer means anything
        s.execute(delete(AlertState).where(AlertState.rule_id == r.id))
    r.event, r.options, r.enabled, r.channels = d["event"], d["options"], d["enabled"], chans
    r.scope = normalize_scope(d["event"], scope)


_APPLY = {"settings": _apply_setting, "connections": _apply_connection, "service_checks": _apply_check,
          "pipelines": _apply_pipeline, "alert_channels": _apply_channel, "alert_rules": _apply_rule}


def apply(session: Session, user: User, doc: Any, passphrase: str | None, picked: list[str]) -> list[dict[str, Any]]:
    """Apply the picked objects in SECTIONS order, each committed on its own: [{key, section, name, action, ok, error}]."""
    p = plan(session, doc, passphrase)
    want = set(picked)
    results = []
    for it in sorted((i for i in p.items if i.key in want), key=lambda i: SECTIONS.index(i.section)):
        res = {"key": it.key, "section": it.section, "name": it.name, "action": it.action, "ok": False, "error": None}
        results.append(res)
        if it.action not in ("create", "update"):
            res["error"] = "Nothing to do: the same here" if it.action == "skip" else "Can't be applied: " + "; ".join(it.notes)
            continue
        try:
            _APPLY[it.section](session, user, it)
            session.commit()
            res["ok"] = True
        except ItemError as e:
            session.rollback()
            res["error"] = str(e)
        except Exception as e:  # noqa: BLE001 - one object failing must not stop the others
            session.rollback()
            res["error"] = short_error(e)
    runtime.invalidate()
    return results
