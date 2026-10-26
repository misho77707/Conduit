"""Alerts (Control & Setup): channels, rules, the mute, and what is firing now.

Admins and superadmins manage channels and rules; everyone (viewers too) may read the active alerts, which is what
the Monitor's panel shows. Secrets are Fernet-encrypted and never returned; a blank secret on edit keeps the stored
one. A Test sends a real message now and returns the real error."""
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..alerts import delivery, engine, senders
from ..alerts.catalog import EVENTS, normalize_options, normalize_scope
from ..alerts.messages import aware, test_message
from ..db import get_session
from ..deps import admin_only, is_viewer, viewer_ok
from ..models import AlertChannel, AlertRule, AlertState, Setting, User, utcnow
from ..schemas import (ActiveAlertOut, ActiveAlertsOut, AlertChannelIn, AlertChannelOut, AlertChannelPatch,
                       AlertChannelTestIn, AlertMuteIn, AlertMuteOut, AlertRuleIn, AlertRuleOut, AlertRulePatch,
                       AlertSendResult, AlertTestIn)
from ..security import encrypt_secrets

router = APIRouter(prefix="/alerts", tags=["alerts"])
MAX_MUTE = timedelta(days=7)


# ---- channels -------------------------------------------------------------------------------------------------

def _channel_out(ch: AlertChannel) -> AlertChannelOut:
    secrets = delivery.channel_secrets(ch)
    return AlertChannelOut(id=ch.id, name=ch.name, kind=ch.kind, config=ch.config or {}, secrets_set=sorted(secrets),
                           enabled=ch.enabled, last_sent_at=ch.last_sent_at, last_error=ch.last_error,
                           rules=sorted(r.name for r in ch.rules), created_at=ch.created_at, updated_at=ch.updated_at)


def _channel_or_404(session: Session, channel_id: uuid.UUID) -> AlertChannel:
    ch = session.get(AlertChannel, channel_id)
    if ch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel not found")
    return ch


def _checked(kind: str, config: dict, secrets: dict) -> tuple[dict, dict]:
    try:
        return senders.check_channel(kind, config, secrets)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


def _commit_or_409(session: Session, what: str, name: str) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"There is already {what} called {name!r}") from None


@router.get("/channels", dependencies=[Depends(admin_only)])
def list_channels(session: Session = Depends(get_session)) -> list[AlertChannelOut]:
    return [_channel_out(c) for c in session.scalars(select(AlertChannel).order_by(AlertChannel.name))]


@router.post("/channels", status_code=status.HTTP_201_CREATED, dependencies=[Depends(admin_only)])
def create_channel(body: AlertChannelIn, session: Session = Depends(get_session)) -> AlertChannelOut:
    config, secrets = _checked(body.kind, body.config, body.secrets)
    ch = AlertChannel(name=body.name.strip(), kind=body.kind, config=config, enabled=body.enabled,
                      secret_enc=encrypt_secrets(secrets) if secrets else "")
    session.add(ch)
    _commit_or_409(session, "a channel", ch.name)
    return _channel_out(ch)


def _merged_secrets(ch: AlertChannel | None, new: dict | None, clear: list[str] = ()) -> dict:
    old = delivery.channel_secrets(ch) if ch else {}
    merged = {**old, **{k: v for k, v in (new or {}).items() if v not in (None, "", {})}}
    return {k: v for k, v in merged.items() if k not in clear}


@router.patch("/channels/{channel_id}", dependencies=[Depends(admin_only)])
def update_channel(channel_id: uuid.UUID, body: AlertChannelPatch, session: Session = Depends(get_session)) -> AlertChannelOut:
    ch = _channel_or_404(session, channel_id)
    config = {**(ch.config or {}), **body.config} if body.config is not None else dict(ch.config or {})
    config, secrets = _checked(ch.kind, config, _merged_secrets(ch, body.secrets, body.clear_secrets))
    ch.config, ch.secret_enc = config, encrypt_secrets(secrets) if secrets else ""
    if body.name is not None:
        ch.name = body.name.strip()
    if body.enabled is not None:
        ch.enabled = body.enabled
    _commit_or_409(session, "a channel", ch.name)
    return _channel_out(ch)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(admin_only)])
def delete_channel(channel_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    session.delete(_channel_or_404(session, channel_id))  # its rules just stop sending here
    session.commit()


def _send_now(kind: str, config: dict, secrets: dict, rule_name: str | None = None) -> str | None:
    try:
        senders.send(kind, config, secrets, test_message(rule_name))
        return None
    except senders.SendError as e:
        return str(e)


@router.post("/channels/test", dependencies=[Depends(admin_only)])
async def test_unsaved_channel(body: AlertChannelTestIn, session: Session = Depends(get_session)) -> AlertSendResult:
    """Send a sample message with these settings before saving them (blank secrets: the saved channel's)."""
    saved = session.get(AlertChannel, body.id) if body.id else None
    config = {**(saved.config or {}), **body.config} if saved else body.config
    config, secrets = _checked(body.kind, config, _merged_secrets(saved, body.secrets))
    err = await run_in_threadpool(_send_now, body.kind, config, secrets)
    return AlertSendResult(channel_id=body.id, name=body.name, ok=err is None, error=err)


def _test_saved(session: Session, ch: AlertChannel, rule_name: str | None) -> AlertSendResult:
    err = _send_now(ch.kind, dict(ch.config or {}), delivery.channel_secrets(ch), rule_name)
    delivery.record(ch.id, err)
    return AlertSendResult(channel_id=ch.id, name=ch.name, ok=err is None, error=err)


@router.post("/channels/{channel_id}/test", dependencies=[Depends(admin_only)])
async def test_channel(channel_id: uuid.UUID, session: Session = Depends(get_session)) -> AlertSendResult:
    """Send a sample message now; the result is also stored as the channel's last send."""
    ch = _channel_or_404(session, channel_id)
    return await run_in_threadpool(_test_saved, session, ch, None)


# ---- rules ----------------------------------------------------------------------------------------------------

def _firing_counts(session: Session) -> dict[uuid.UUID, int]:
    rows = session.execute(select(AlertState.rule_id, func.count()).where(AlertState.firing.is_(True))
                           .group_by(AlertState.rule_id))
    return {rid: n for rid, n in rows}


def _rule_out(r: AlertRule, firing: int) -> AlertRuleOut:
    return AlertRuleOut(id=r.id, name=r.name, event=r.event, event_label=EVENTS[r.event].label, scope=r.scope or {},
                        options=r.options or {}, channel_ids=[c.id for c in r.channels], enabled=r.enabled,
                        firing=firing, created_at=r.created_at, updated_at=r.updated_at)


def _rule_or_404(session: Session, rule_id: uuid.UUID) -> AlertRule:
    r = session.get(AlertRule, rule_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    return r


def _channels(session: Session, ids: list[uuid.UUID]) -> list[AlertChannel]:
    found = list(session.scalars(select(AlertChannel).where(AlertChannel.id.in_(ids)))) if ids else []
    if len(found) != len(set(ids)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "One of the channels no longer exists")
    return found


def _rule_parts(event: str, scope: dict | None, options: dict | None) -> tuple[dict, dict]:
    try:
        return normalize_scope(event, scope), normalize_options(event, options)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


def _forget(session: Session, rule: AlertRule) -> None:
    """Drop a rule's current state, without messages (it was edited in a way that makes the old state meaningless)."""
    for st in session.scalars(select(AlertState).where(AlertState.rule_id == rule.id)):
        session.delete(st)


@router.get("/rules", dependencies=[Depends(admin_only)])
def list_rules(session: Session = Depends(get_session)) -> list[AlertRuleOut]:
    counts = _firing_counts(session)
    return [_rule_out(r, counts.get(r.id, 0)) for r in session.scalars(select(AlertRule).order_by(AlertRule.name))]


@router.post("/rules", status_code=status.HTTP_201_CREATED, dependencies=[Depends(admin_only)])
def create_rule(body: AlertRuleIn, session: Session = Depends(get_session)) -> AlertRuleOut:
    scope, options = _rule_parts(body.event, body.scope, body.options)
    r = AlertRule(name=body.name.strip(), event=body.event, scope=scope, options=options, enabled=body.enabled,
                  channels=_channels(session, body.channel_ids))
    session.add(r)
    _commit_or_409(session, "a rule", r.name)
    return _rule_out(r, 0)


@router.patch("/rules/{rule_id}", dependencies=[Depends(admin_only)])
def update_rule(rule_id: uuid.UUID, body: AlertRulePatch, session: Session = Depends(get_session)) -> AlertRuleOut:
    r = _rule_or_404(session, rule_id)
    event = body.event or r.event
    if event != r.event:
        _forget(session, r)
    scope, options = _rule_parts(event, body.scope if body.scope is not None else (r.scope if event == r.event else None),
                                 body.options if body.options is not None else (r.options if event == r.event else None))
    r.event, r.scope, r.options = event, scope, options
    if body.name is not None:
        r.name = body.name.strip()
    if body.channel_ids is not None:
        r.channels = _channels(session, body.channel_ids)
    if body.enabled is not None:
        if not body.enabled:
            _forget(session, r)
        r.enabled = body.enabled
    _commit_or_409(session, "a rule", r.name)
    return _rule_out(r, _firing_counts(session).get(r.id, 0))


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(admin_only)])
def delete_rule(rule_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    r = _rule_or_404(session, rule_id)
    _forget(session, r)
    session.delete(r)
    session.commit()


@router.post("/test", dependencies=[Depends(admin_only)])
async def test_channels(body: AlertTestIn, session: Session = Depends(get_session)) -> list[AlertSendResult]:
    """The rule editor's "Send test": a sample message to each of these channels now."""
    chans = _channels(session, body.channel_ids)
    return [await run_in_threadpool(_test_saved, session, ch, body.rule_name) for ch in chans]


# ---- mute ----------------------------------------------------------------------------------------------------

@router.get("/mute", dependencies=[Depends(admin_only)])
def get_mute(session: Session = Depends(get_session)) -> AlertMuteOut:
    until = engine.mute_until(session)
    return AlertMuteOut(until=until if until and until > utcnow() else None)


@router.put("/mute")
def set_mute(body: AlertMuteIn, session: Session = Depends(get_session), user: User = Depends(admin_only)) -> AlertMuteOut:
    """Mute every alert until a time (at most 7 days ahead), or unmute now (null): one summary of what is still
    firing goes out when a mute ends."""
    now = utcnow()
    row = session.get(Setting, engine.MUTE_KEY)
    if body.until is None:
        out = engine.Outbox()
        if row is not None:
            session.delete(row)
            engine.summarize(session, out)
        session.commit()
        delivery.enqueue(out.items)
        return AlertMuteOut(until=None)
    until = aware(body.until)
    if not now < until <= now + MAX_MUTE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Mute until a time in the next 7 days")
    if row is None:
        row = Setting(key=engine.MUTE_KEY)
        session.add(row)
    row.value, row.updated_by_id = until.isoformat(), user.id
    session.commit()
    return AlertMuteOut(until=until)


# ---- what is firing now (the Monitor panel; viewers too) -------------------------------------------------------

@router.get("/active")
def active_alerts(session: Session = Depends(get_session), user: User = Depends(viewer_ok)) -> ActiveAlertsOut:
    viewer = is_viewer(user)
    rules = {r.id: r for r in session.scalars(select(AlertRule).where(AlertRule.enabled.is_(True)))}
    items = []
    for st in session.scalars(select(AlertState).where(AlertState.firing.is_(True)).order_by(AlertState.bad_since)):
        r = rules.get(st.rule_id)
        if r is None:
            continue
        items.append(ActiveAlertOut(
            rule_id=None if viewer else r.id, rule_name=r.name, event=r.event, event_label=EVENTS[r.event].label,
            subject_kind=st.subject_kind, subject_id=None if viewer else st.subject_id, subject_label=st.subject_label,
            since=aware(st.bad_since), fired_at=aware(st.fired_at), reason=st.reason, link=st.link))
    until = engine.mute_until(session)
    return ActiveAlertsOut(muted_until=until if until and until > utcnow() else None, alerts=items)
