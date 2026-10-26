"""The four channel kinds against local servers: http.server for Telegram / Slack / webhook, aiosmtpd for email."""
import hashlib
import hmac
import json
import socket
import time

import pytest

from alerthelpers import Sink


@pytest.fixture()
def sink():
    s = Sink()
    yield s
    s.close()


@pytest.fixture()
def msg():
    from app.alerts.messages import Message

    return Message(status="firing", title="Pipeline failed: nightly-orders", lines=["Scheduled run failed · since 26 Oct 02:00 UTC"],
                   detail='relation "public.orders_stage" does not exist', link="https://conduit.example/#/jobs/1",
                   event="job_failed", rule={"id": "r1", "name": "Failed scheduled pipelines"},
                   subject={"kind": "pipeline", "id": "p1", "name": "nightly-orders"}, since="2026-10-26T02:00:04+00:00")


def test_telegram_posts_html_to_the_bot_api_and_hides_the_token_in_errors(env, sink, msg, monkeypatch):
    from app.alerts import senders
    from app.config import get_settings

    monkeypatch.setenv("CONDUIT_TELEGRAM_API_URL", sink.url)
    get_settings.cache_clear()
    token = "123456:secret-bot-token"
    senders.send("telegram", {"chat_id": "-1001"}, {"bot_token": token}, msg)
    r = sink.requests[0]
    assert r["path"] == f"/bot{token}/sendMessage"
    assert r["json"]["chat_id"] == "-1001" and r["json"]["parse_mode"] == "HTML"
    assert r["json"]["text"].startswith("🔴 <b>Pipeline failed: nightly-orders</b>") and "<pre>relation" in r["json"]["text"]

    sink.reply = (400, b'{"ok": false, "description": "Bad Request: chat not found"}')
    with pytest.raises(senders.SendError) as e:
        senders.send("telegram", {"chat_id": "-1001"}, {"bot_token": token}, msg)
    assert str(e.value) == "Telegram: Bad Request: chat not found" and token not in str(e.value)


def test_slack_posts_mrkdwn_and_hides_the_webhook_url(env, sink, msg):
    from app.alerts import senders

    url = f"{sink.url}/services/T0/B0/xyzSECRET"
    senders.send("slack", {}, {"webhook_url": url}, msg)
    text = sink.requests[0]["json"]["text"]
    assert text.startswith(":red_circle: *Pipeline failed: nightly-orders*") and "```relation" in text
    assert "<https://conduit.example/#/jobs/1|Open in Conduit>" in text
    sink.reply = (404, b"no_service")
    with pytest.raises(senders.SendError) as e:
        senders.send("slack", {}, {"webhook_url": url}, msg)
    assert "Slack: HTTP 404 no_service" in str(e.value) and "xyzSECRET" not in str(e.value)


def test_webhook_payload_headers_and_hmac_signature(env, sink, msg):
    from app.alerts import senders

    secret = "s3cret-signing-key"
    senders.send("webhook", {}, {"url": f"{sink.url}/hook?team=data", "headers": {"X-Team": "data"},
                                 "signing_secret": secret}, msg)
    r = sink.requests[0]
    assert r["path"] == "/hook?team=data" and r["headers"]["X-Team"] == "data"
    expected = "sha256=" + hmac.new(secret.encode(), r["raw"], hashlib.sha256).hexdigest()
    assert r["headers"]["X-Conduit-Signature"] == expected
    p = r["json"]
    assert (p["version"], p["status"], p["event"], p["title"]) == (1, "firing", "job_failed", "Pipeline failed: nightly-orders")
    assert p["subject"] == {"kind": "pipeline", "id": "p1", "name": "nightly-orders"} and p["instance"] == "Conduit"
    assert p["url"] == "https://conduit.example/#/jobs/1" and p["sent_at"].endswith("Z")


def test_a_webhook_without_a_secret_is_unsigned(env, sink, msg):
    from app.alerts import senders

    senders.send("webhook", {}, {"url": f"{sink.url}/h"}, msg)
    assert "X-Conduit-Signature" not in sink.requests[0]["headers"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_email_over_smtp_with_login_text_and_html(env, msg):
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult

    from app.alerts import senders

    got = []

    class Handler:
        async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
            if address.startswith("nobody@"):
                return "550 5.1.1 No such user here"
            envelope.rcpt_tos.append(address)
            return "250 OK"

        async def handle_DATA(self, server, session, envelope):  # noqa: N802
            got.append(envelope)
            return "250 OK"

    def auth(server, session, envelope, mechanism, data):
        return AuthResult(success=data.login == b"alerts" and data.password == b"smtp-pass")

    port = _free_port()
    ctl = Controller(Handler(), hostname="127.0.0.1", port=port, authenticator=auth, auth_require_tls=False)
    ctl.start()
    try:
        config, secrets = senders.check_channel("email", {"host": "127.0.0.1", "port": port, "tls": "none", "user": "alerts",
                                                          "from": "conduit@example.com", "to": "a@example.com, b@example.com"},
                                                {"password": "smtp-pass"})
        senders.send("email", config, secrets, msg)
        time.sleep(0.2)
        env_ = got[0]
        assert sorted(env_.rcpt_tos) == ["a@example.com", "b@example.com"] and env_.mail_from == "conduit@example.com"
        body = env_.content.decode()
        assert "Subject: [Conduit] Pipeline failed: nightly-orders" in body
        assert "text/plain" in body and "text/html" in body and "relation" in body

        with pytest.raises(senders.SendError) as e:  # the server refuses: its reason comes back, the password doesn't
            senders.send("email", {**config, "to": ["nobody@example.com"]}, secrets, msg)
        assert str(e.value).startswith("SMTP: SMTPRecipientsRefused") and "No such user" in str(e.value)
        assert "smtp-pass" not in str(e.value)
    finally:
        ctl.stop()


def test_an_unreachable_server_is_a_readable_error(env, msg):
    from app.alerts import senders

    with pytest.raises(senders.SendError) as e:
        senders.send("webhook", {}, {"url": f"http://127.0.0.1:{_free_port()}/x"}, msg)
    assert str(e.value).startswith("Webhook: ConnectError")


@pytest.mark.parametrize("kind, config, secrets, says", [
    ("telegram", {}, {"bot_token": "x"}, "chat ID"),
    ("telegram", {"chat_id": "1"}, {}, "bot token"),
    ("slack", {}, {"webhook_url": "http://hooks.slack.com/x"}, "https://"),
    ("email", {"host": "h", "from": "a@b", "to": []}, {}, "recipients"),
    ("email", {"host": "h", "from": "nope", "to": ["a@b"]}, {}, "From address"),
    ("email", {"host": "h", "tls": "maybe", "from": "a@b", "to": ["a@b"]}, {}, "tls must be"),
    ("webhook", {}, {"url": "ftp://x"}, "http(s)://"),
    ("webhook", {}, {"url": "https://x", "headers": {"A": 1}}, "headers"),
])
def test_channel_settings_are_checked(kind, config, secrets, says):
    from app.alerts import senders

    with pytest.raises(ValueError) as e:
        senders.check_channel(kind, config, secrets)
    assert says in str(e.value)


def test_messages_mask_query_strings_and_cut_long_errors(env):
    from app.alerts.messages import clean

    assert clean("GET https://u:p@files.example.com/a.csv?X-Amz-Signature=abc failed") == \
        "GET https://files.example.com/a.csv failed"
    long = clean("x" * 2000)
    assert len(long) == 500 and long.endswith("…")
    assert json.dumps(clean("sftp://bob:pw@host:22/in/f.csv")) == '"sftp://host:22/in/f.csv"'
