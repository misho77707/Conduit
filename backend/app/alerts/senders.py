"""Sending one message to one channel: Telegram, Slack, email (SMTP) or a webhook. Each raises SendError with a
readable reason; the channel's own secrets (bot token, webhook URL, password) never appear in it."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

import httpx

from ..config import get_settings
from . import messages
from .messages import Message

KINDS = ("telegram", "slack", "email", "webhook")
SECRET_FIELDS = {"telegram": ("bot_token",), "slack": ("webhook_url",), "email": ("password",),
                 "webhook": ("url", "headers", "signing_secret")}
TLS_MODES = ("starttls", "ssl", "none")
DEFAULT_PORT = {"starttls": 587, "ssl": 465, "none": 25}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


class SendError(Exception):
    """A send failed; the message is safe to show and store (channel.last_error)."""


def _mask(text: str, secrets: dict[str, Any]) -> str:
    for v in secrets.values():
        for s in (v.values() if isinstance(v, dict) else [v]):
            if isinstance(s, str) and len(s) >= 4:
                text = text.replace(s, "***")
    return text[:480]


# ---- validation (on save) ---------------------------------------------------------------------------------

def _url(v: Any, what: str, https_only: bool = False) -> str:
    v = str(v or "").strip()
    ok = v.startswith("https://") or (not https_only and v.startswith("http://"))
    if not ok:
        raise ValueError(f"{what} must be an {'https' if https_only else 'http(s)'}:// URL")
    return v


def check_channel(kind: str, config: dict[str, Any], secrets: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Cleaned (config, secrets) for a channel of this kind; ValueError says what is missing or wrong. `secrets` is
    the effective set (stored ones merged with the edited ones)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    config, secrets = dict(config or {}), {k: v for k, v in (secrets or {}).items() if k in SECRET_FIELDS[kind] and v not in (None, "", {})}
    if kind == "telegram":
        chat = str(config.get("chat_id") or "").strip()
        if not chat:
            raise ValueError("Telegram needs a chat ID (a number like -1001234567890, or @channelname)")
        if not secrets.get("bot_token"):
            raise ValueError("Telegram needs the bot token (from BotFather)")
        return {"chat_id": chat}, secrets
    if kind == "slack":
        secrets["webhook_url"] = _url(secrets.get("webhook_url"), "The Slack webhook URL", https_only=True)
        return {}, secrets
    if kind == "email":
        tls = config.get("tls") or "starttls"
        if tls not in TLS_MODES:
            raise ValueError("tls must be one of: starttls, ssl, none")
        host = str(config.get("host") or "").strip()
        if not host:
            raise ValueError("Email needs the SMTP host")
        try:
            port = int(config.get("port") or DEFAULT_PORT[tls])
        except (TypeError, ValueError):
            raise ValueError("The SMTP port must be a number") from None
        if not 1 <= port <= 65535:
            raise ValueError("The SMTP port must be between 1 and 65535")
        to = config.get("to") or []
        to = [x.strip() for x in (to.split(",") if isinstance(to, str) else to) if str(x).strip()]
        sender = str(config.get("from") or "").strip()
        if not _EMAIL.match(sender):
            raise ValueError("Email needs a From address")
        bad = [x for x in to if not _EMAIL.match(x)]
        if not to or bad:
            raise ValueError("Email needs one or more recipients" + (f" ({', '.join(bad)} is not an address)" if bad else ""))
        return {"host": host, "port": port, "tls": tls, "user": str(config.get("user") or "").strip(),
                "from": sender, "to": to}, secrets
    # webhook
    secrets["url"] = _url(secrets.get("url"), "The webhook URL")
    headers = secrets.get("headers") or {}
    if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        raise ValueError("Webhook headers must be name: value text pairs")
    if headers:
        secrets["headers"] = headers
    return {}, secrets


# ---- sending ----------------------------------------------------------------------------------------------

def _post(url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    with httpx.Client(timeout=get_settings().alert_send_timeout_s, follow_redirects=False) as c:
        return c.post(url, content=body, headers={"Content-Type": "application/json", "User-Agent": "Conduit-Alerts/1", **headers})


def _telegram(config: dict[str, Any], secrets: dict[str, Any], m: Message) -> None:
    api = get_settings().telegram_api_url.rstrip("/")
    body = json.dumps({"chat_id": config["chat_id"], "text": messages.telegram(m), "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    r = _post(f"{api}/bot{secrets['bot_token']}/sendMessage", body, {})
    if r.status_code != 200:
        try:
            why = r.json().get("description") or r.text
        except ValueError:
            why = r.text
        raise SendError(f"Telegram: {why or f'HTTP {r.status_code}'}")


def _slack(config: dict[str, Any], secrets: dict[str, Any], m: Message) -> None:
    r = _post(secrets["webhook_url"], json.dumps(messages.slack(m)).encode(), {})
    if r.status_code != 200:
        raise SendError(f"Slack: HTTP {r.status_code} {r.text[:200]}".strip())


def sign(secret: str, body: bytes) -> str:
    """The X-Conduit-Signature header value: sha256=<hex HMAC-SHA256 of the raw body>."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook(config: dict[str, Any], secrets: dict[str, Any], m: Message) -> None:
    payload = messages.webhook(m, get_settings().instance_name, datetime.now(timezone.utc))
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = dict(secrets.get("headers") or {})
    if secrets.get("signing_secret"):
        headers["X-Conduit-Signature"] = sign(secrets["signing_secret"], body)
    r = _post(secrets["url"], body, headers)
    if not 200 <= r.status_code < 300:
        raise SendError(f"HTTP {r.status_code} {r.text[:200]}".strip())


def _email(config: dict[str, Any], secrets: dict[str, Any], m: Message) -> None:
    instance = get_settings().instance_name
    subject, text, html_body = messages.email(m, instance)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, config["from"], ", ".join(config["to"])
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    timeout = get_settings().alert_send_timeout_s
    host, port, tls = config["host"], int(config["port"]), config.get("tls", "starttls")
    smtp: smtplib.SMTP = (smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
                          if tls == "ssl" else smtplib.SMTP(host, port, timeout=timeout))
    try:
        if tls == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if config.get("user"):
            smtp.login(config["user"], secrets.get("password") or "")
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001
            pass


SENDERS = {"telegram": _telegram, "slack": _slack, "email": _email, "webhook": _webhook}


def send(kind: str, config: dict[str, Any], secrets: dict[str, Any], m: Message) -> None:
    """Send now; SendError on any failure (network, refused, a bad token ...), with secrets masked."""
    try:
        SENDERS[kind](config, secrets, m)
    except SendError as e:
        raise SendError(_mask(str(e), secrets)) from None
    except smtplib.SMTPAuthenticationError as e:
        raise SendError(_mask(f"SMTP: authentication failed ({e.smtp_code})", secrets)) from None
    except (smtplib.SMTPException, OSError, httpx.HTTPError, ssl.SSLError) as e:
        what = {"email": "SMTP", "telegram": "Telegram", "slack": "Slack"}.get(kind, "Webhook")
        raise SendError(_mask(f"{what}: {type(e).__name__}: {e}", secrets)) from None
