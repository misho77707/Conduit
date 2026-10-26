"""Alert messages: one neutral Message, rendered for Telegram (HTML), Slack (mrkdwn), email (text + HTML) and the
webhook (documented JSON). Short and readable on a phone; URLs lose their query string; errors are cut at ~500
characters; links only when public_url is set."""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import get_settings
from ..connectors.http import mask_url

EXCERPT = 500
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
EMOJI = {"firing": "🔴", "recovered": "✅", "summary": "🔔", "test": "🔔"}
SLACK_EMOJI = {"firing": ":red_circle:", "recovered": ":white_check_mark:", "summary": ":bell:", "test": ":bell:"}


@dataclass
class Message:
    status: str  # firing | recovered | summary | test
    title: str  # "Pipeline failed: nightly-orders"
    lines: list[str] = field(default_factory=list)  # short plain lines under the title
    detail: str | None = None  # an error excerpt, shown as code
    link: str | None = None  # absolute URL into Conduit (None when public_url isn't set)
    link_label: str = "Open in Conduit"
    event: str | None = None
    rule: dict[str, str] | None = None  # {"id", "name"}
    subject: dict[str, str] | None = None  # {"kind", "id", "name"}
    since: str | None = None  # ISO time the problem began
    reason: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)  # a summary's entries: {"title", "since", "status"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---- text helpers ------------------------------------------------------------------------------------------

def aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def fmt_time(d: datetime | None) -> str:
    d = aware(d)
    return d.astimezone(timezone.utc).strftime("%d %b %H:%M UTC").lstrip("0") if d else "?"


def fmt_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d} d {h} h" if h else f"{d} d"
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    if m:
        return f"{m} min"
    return f"{s} s"


def clean(text: str | None, limit: int = EXCERPT) -> str:
    """One string safe to send: URLs without their query string or credentials, at most `limit` characters."""
    t = _URL.sub(lambda m: mask_url(m.group(0)), (text or "").strip())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def absolute(path: str | None) -> str | None:
    """"#/jobs/<id>" -> "https://conduit.example/#/jobs/<id>", or None without a public_url."""
    base = (get_settings().public_url or "").strip().rstrip("/")
    return f"{base}/{path.lstrip('/')}" if base and path else None


# ---- per channel ----------------------------------------------------------------------------------------

def _summary_lines(m: Message) -> list[str]:
    out = []
    for it in m.items:
        mark = "recovered" if it.get("status") == "recovered" else "since " + it.get("since", "?")
        out.append(f"• {it['title']} ({mark})")
    return out


def telegram(m: Message) -> str:
    e = html.escape
    parts = [f"{EMOJI.get(m.status, '')} <b>{e(m.title)}</b>".strip()]
    parts += [e(x) for x in m.lines + _summary_lines(m)]
    if m.detail:
        parts.append(f"<pre>{e(m.detail)}</pre>")
    if m.link:
        parts.append(f'<a href="{e(m.link, quote=True)}">{e(m.link_label)}</a>')
    text = "\n".join(parts)
    return text if len(text) <= 4000 else text[:3990] + "…"


def _slack_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack(m: Message) -> dict[str, Any]:
    parts = [f"{SLACK_EMOJI.get(m.status, '')} *{_slack_escape(m.title)}*".strip()]
    parts += [_slack_escape(x) for x in m.lines + _summary_lines(m)]
    if m.detail:
        parts.append(f"```{_slack_escape(m.detail)}```")
    if m.link:
        parts.append(f"<{m.link}|{_slack_escape(m.link_label)}>")
    return {"text": "\n".join(parts)}


def email(m: Message, instance: str) -> tuple[str, str, str]:
    """(subject, plain text, HTML)."""
    subject = f"[{instance}] {m.title}"
    body = [m.title, *m.lines, *_summary_lines(m)]
    if m.detail:
        body += ["", m.detail]
    if m.link:
        body += ["", f"{m.link_label}: {m.link}"]
    body += ["", f"-- {instance}"]
    e = html.escape
    color = {"firing": "#c0392b", "recovered": "#1e8449"}.get(m.status, "#2c3e50")
    h = [f'<h3 style="margin:0 0 8px;color:{color};font-family:sans-serif">{e(m.title)}</h3>']
    h += [f'<div style="font-family:sans-serif">{e(x)}</div>' for x in m.lines + _summary_lines(m)]
    if m.detail:
        h.append(f'<pre style="background:#f4f4f4;padding:8px;white-space:pre-wrap">{e(m.detail)}</pre>')
    if m.link:
        h.append(f'<p style="font-family:sans-serif"><a href="{e(m.link, quote=True)}">{e(m.link_label)}</a></p>')
    h.append(f'<p style="font-family:sans-serif;color:#888;font-size:12px">{e(instance)}</p>')
    return subject, "\n".join(body), "\n".join(h)


def webhook(m: Message, instance: str, sent_at: datetime) -> dict[str, Any]:
    """The documented webhook payload (README, Control & Setup)."""
    return {
        "version": 1, "status": m.status, "event": m.event, "title": m.title,
        "rule": m.rule, "subject": m.subject, "since": m.since, "reason": m.reason,
        "url": m.link, "items": m.items or None, "instance": instance,
        "sent_at": aware(sent_at).isoformat().replace("+00:00", "Z"),
    }


def test_message(rule_name: str | None = None) -> Message:
    title = f"Test from {get_settings().instance_name}" + (f": {rule_name}" if rule_name else "")
    return Message(status="test", title=title, lines=["If you can read this, alerts reach this channel."],
                   link=absolute("#/control"), link_label="Open Control & Setup")
