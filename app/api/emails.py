from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.database.models import Email, EmailAnalysis
from app.services.settings_service import get_runtime_settings


router = APIRouter(prefix="/emails", tags=["emails"])


def _analysis_dict(analysis: EmailAnalysis, email: Email):
    return {
        "email_id": email.id,
        "analysis_id": analysis.id,
        "sender": analysis.sender,
        "subject": analysis.subject,
        "received_at": email.received_at,
        "importance": analysis.importance,
        "category": analysis.category,
        "needs_reply": analysis.needs_reply,
        "needs_action": analysis.needs_action,
        "urgency": analysis.urgency,
        "summary": analysis.summary,
        "why_important": analysis.why_important,
        "requested_action": analysis.requested_action,
        "deadline": analysis.deadline,
        "key_facts": analysis.key_facts_json,
        "risk_flags": analysis.risk_flags_json,
        "confidence": analysis.confidence,
    }


@router.get("/important")
async def important_emails(db: Session = Depends(get_db)):
    provider = get_runtime_settings().email_provider
    rows = db.execute(
        select(EmailAnalysis, Email)
        .join(Email, Email.id == EmailAnalysis.email_id)
        .where(
            Email.provider == provider,
            EmailAnalysis.importance.in_(["CRITICAL", "HIGH", "MEDIUM"]),
        )
        .order_by(EmailAnalysis.created_at.desc())
    ).all()
    return [_analysis_dict(analysis, email) for analysis, email in rows]


@router.get("/{email_id}/analysis")
async def email_analysis(email_id: int, db: Session = Depends(get_db)):
    row = db.execute(
        select(EmailAnalysis, Email)
        .join(Email, Email.id == EmailAnalysis.email_id)
        .where(Email.id == email_id)
        .order_by(EmailAnalysis.created_at.desc())
        .limit(1)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Email analysis not found")
    return _analysis_dict(*row)
