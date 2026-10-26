"""Settings (Control & Setup): the configuration admins may change without a restart, and where each value comes from.

Runtime settings are overridden here (a row in `settings`, seen by every process within runtime.CACHE_S);
restart-required ones are read-only (set in .env, then restart) and hidden ones (secrets, connection strings) never
appear. Effective value: the UI override, else .env, else the default. See app/runtime.py."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import runtime
from ..db import get_session
from ..deps import admin_only
from ..models import Setting, User, utcnow
from ..schemas import SettingIn, SettingOut

router = APIRouter(prefix="/settings", tags=["settings"])
ORDER = list(runtime.GROUPS)


def _one(session: Session, key: str) -> SettingOut:
    return SettingOut(**runtime.describe(session, key)[0])


def _spec_or_404(key: str) -> runtime.Spec:
    spec = runtime.SPECS.get(key)
    if spec is None or spec.cls == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such setting")
    return spec


@router.get("", dependencies=[Depends(admin_only)])
def list_settings(session: Session = Depends(get_session)) -> list[SettingOut]:
    items = sorted(runtime.describe(session), key=lambda d: ORDER.index(d["group"]))
    return [SettingOut(**d) for d in items]


@router.put("/{key}")
def set_setting(key: str, body: SettingIn, session: Session = Depends(get_session),
                user: User = Depends(admin_only)) -> SettingOut:
    spec = _spec_or_404(key)
    if spec.cls != "runtime":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"{spec.label} is set in .env (CONDUIT_{key.upper()}) and needs a restart")
    try:
        value = runtime.check(key, body.value)
    except runtime.SettingError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        session.add(row)
    row.value, row.updated_by_id, row.updated_at = value, user.id, utcnow()
    session.commit()
    runtime.invalidate()
    return _one(session, key)


@router.delete("/{key}", dependencies=[Depends(admin_only)])
def reset_setting(key: str, session: Session = Depends(get_session)) -> SettingOut:
    """Back to .env or the default: the override is removed."""
    _spec_or_404(key)
    row = session.get(Setting, key)
    if row is not None:
        session.delete(row)
        session.commit()
    runtime.invalidate()
    return _one(session, key)
