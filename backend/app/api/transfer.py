"""Export / import (Control & Setup): the configuration as one JSON file; import is a dry run, then the picked objects.

Admins and superadmins. Secrets leave only on request, only for a superadmin (admins can use saved credentials but
never read them), and only encrypted with a passphrase. See app/transfer.py for what the file holds and the rules
an import follows."""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from .. import transfer
from ..db import get_session
from ..deps import admin_only
from ..models import User, utcnow
from ..runtime import current_settings
from ..schemas import ExportIn, ImportApplyIn, ImportIn, ImportPlanOut, ImportResultOut

router = APIRouter(tags=["transfer"])


@router.post("/export")
def export_file(body: ExportIn, session: Session = Depends(get_session), user: User = Depends(admin_only)) -> Response:
    if body.include_secrets and user.role != "superadmin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a superadmin may export secrets")
    if body.include_secrets and not body.passphrase:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Exporting secrets needs a passphrase")
    try:
        doc = transfer.export(session, body.passphrase if body.include_secrets else None)
    except transfer.TransferError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    slug = re.sub(r"[^a-z0-9]+", "-", current_settings().instance_name.lower()).strip("-") or "conduit"
    name = f"{slug}-{utcnow().strftime('%Y%m%d-%H%M')}.json"
    return Response(json.dumps(doc, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/import/plan", dependencies=[Depends(admin_only)])
def import_plan(body: ImportIn, session: Session = Depends(get_session)) -> ImportPlanOut:
    """The dry run: what each object in the file would do here (create / update / skip / conflict), and why."""
    try:
        return ImportPlanOut(**transfer.plan(session, body.document, body.passphrase).public())
    except transfer.TransferError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


@router.post("/import/apply")
def import_apply(body: ImportApplyIn, session: Session = Depends(get_session),
                 user: User = Depends(admin_only)) -> list[ImportResultOut]:
    """Apply the picked objects (settings, connections, checks, pipelines, channels, rules, in that order)."""
    try:
        return [ImportResultOut(**r) for r in transfer.apply(session, user, body.document, body.passphrase, body.picked)]
    except transfer.TransferError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
