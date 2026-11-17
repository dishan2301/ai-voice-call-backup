from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import MailboxContext, require_mailbox_context
from app.database.db import get_db
from app.database.models import ReceptionistSettings
from app.services.receptionist import normalize_allowlist, normalize_phone, caller_allowed, ReceptionistSession

router = APIRouter(prefix="/phone/receptionist", tags=["receptionist"])

class ReceptionistPatch(BaseModel):
    enabled: bool = False
    allowlist: list[str] = Field(default_factory=list, max_length=100)

def owned(db, context):
    row = db.scalar(select(ReceptionistSettings).where(
        ReceptionistSettings.user_id == context.user_id,
        ReceptionistSettings.workspace_id == context.workspace_id,
        ReceptionistSettings.mailbox_id == context.mailbox_id))
    if not row:
        row = ReceptionistSettings(user_id=context.user_id, workspace_id=context.workspace_id,
                                   mailbox_id=context.mailbox_id)
        db.add(row); db.flush()
    return row

@router.get("/settings")
async def get_settings(db: Session = Depends(get_db), context: MailboxContext = Depends(require_mailbox_context)):
    row = owned(db, context); db.commit()
    return {"enabled": row.enabled, "allowlist": row.allowlist_json, "routing": "disabled"}

@router.put("/settings")
async def put_settings(request: ReceptionistPatch, db: Session = Depends(get_db), context: MailboxContext = Depends(require_mailbox_context)):
    row = owned(db, context); row.enabled = request.enabled; row.allowlist_json = normalize_allowlist(request.allowlist)
    db.commit()
    return {"enabled": row.enabled, "allowlist": row.allowlist_json, "routing": "disabled"}

class SimulatedCall(BaseModel):
    caller_id: str = Field(min_length=1, max_length=40)
    session_key: str = Field(min_length=16, max_length=255)

@router.post("/calls/simulate")
async def simulate_call(request: SimulatedCall, db: Session = Depends(get_db), context: MailboxContext = Depends(require_mailbox_context)):
    row = owned(db, context); db.commit()
    caller = normalize_phone(request.caller_id)
    if not caller:
        raise HTTPException(400, "Caller ID is missing or invalid; normal personal-call behavior is preserved")
    if not caller_allowed(row.enabled, caller, row.allowlist_json):
        return {"status": "normal_call", "ai_answered": False, "reason": "caller_not_allowlisted_or_mode_off"}
    session = ReceptionistSession(caller, request.session_key)
    return {"status": "prototype_ready", "ai_answered": False, "caller_id": caller,
            "engine": "gemini_live", "context": session.prompt(), "routing": "simulated_only"}
