import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, get_settings
from app.core.auth import MailboxContext, require_mailbox_context
from app.api.dependencies import create_mail_agent, get_generation_service
from app.database.db import get_db
from app.database.models import AgentAction, AgentAudit, AgentConversation, Briefing, BriefingItem, BriefingRun, Commitment, Email, EmailAnalysis
from app.services.persistent_briefing_service import BriefingGenerationError
from app.agents.requests import handle_mail_request
from app.agents.mail_agent import MailAgent
from app.agents.draft_safety import begin_review, complete_review, recover_stale_sends
from app.core.security import LOOPBACK_NAMES, phone_session_token
from app.core.auth import MailboxContext, require_mailbox_context
from app.services.mailbox_credentials import MailboxCredentialError
from app.email.gmail import GmailConfigurationError
from googleapiclient.errors import HttpError


router = APIRouter(prefix="/phone", tags=["phone"])


class WhatsAppRewriteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    message: str = Field(min_length=1, max_length=4000)
    instruction: str = Field(min_length=1, max_length=300)


@router.post("/whatsapp/rewrite")
async def whatsapp_rewrite(request: WhatsAppRewriteRequest,
                           context: MailboxContext = Depends(require_mailbox_context)):
    from app.agents.whatsapp_rewrite import rewrite_message
    try:
        revised = await run_in_threadpool(rewrite_message, request.message,
                                          request.instruction, get_settings())
    except Exception as exc:
        raise HTTPException(503, "Gemini could not safely rewrite this message. Your original draft is unchanged.") from exc
    return {"message": revised}


@router.post("/refresh")
async def phone_refresh(service=Depends(get_generation_service)):
    try:
        if getattr(getattr(service, "settings", None), "deployment_mode", "personal") == "hosted":
            count = await run_in_threadpool(service.sync_only, datetime.now(timezone.utc))
            return {"status": "synced", "synced_messages": count, "analysis_status": "pending"}
        document = await run_in_threadpool(service.generate, datetime.now(timezone.utc))
        return {"status": "updated", "total_emails": document.total_emails,
                "critical_count": document.critical_count, "high_count": document.high_count}
    except BriefingGenerationError as exc:
        raise HTTPException(status_code=503, detail=f"Mail refresh failed during {exc.stage.lower()}.") from exc
    except MailboxCredentialError as exc:
        raise HTTPException(403, "Mailbox connection is no longer available") from exc
    except (GmailConfigurationError, HttpError) as exc:
        raise HTTPException(503, "Mail provider is unavailable; reconnect may be required") from exc


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    alternatives: list[dict] = Field(default_factory=list, max_length=5)
    input_mode: str = Field(default="text", pattern="^(text|voice)$")
    session_key: str = Field(default="android", min_length=1, max_length=255)
    review_ticket: str = Field(default="", max_length=100)
    reply_message_id: str = Field(default="", max_length=255)
    reply_thread_id: str = Field(default="", max_length=255)


@router.post("/agent")
async def phone_agent(request: AgentRequest,
                      context: MailboxContext = Depends(require_mailbox_context)):
    def agent_factory():
        from app.api.planner import should_handle
        if isinstance(context, MailboxContext) and should_handle(request.message, request.session_key, context):
            from app.api.dependencies import create_assistant
            return create_assistant(context)
        return create_mail_agent(context) if isinstance(context, MailboxContext) else MailAgent()
    return await run_in_threadpool(handle_mail_request, agent_factory,
                                    request.message, request.session_key,
                                    input_mode=request.input_mode, alternatives=request.alternatives,
                                    review_ticket=request.review_ticket,
                                    seed_message_id=request.reply_message_id,
                                    seed_thread_id=request.reply_thread_id)


class ReviewRequest(BaseModel):
    session_key: str = Field(min_length=1, max_length=255)
    review_ticket: str = Field(default="", max_length=100)


@router.post("/drafts/{draft_id}/review")
async def review_draft(draft_id: int, request: ReviewRequest, db: Session = Depends(get_db),
                       context: MailboxContext = Depends(require_mailbox_context)):
    try:
        return begin_review(db, draft_id, request.session_key, context.mailbox_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/review-complete")
async def finish_review(draft_id: int, request: ReviewRequest, db: Session = Depends(get_db),
                        context: MailboxContext = Depends(require_mailbox_context)):
    try:
        return complete_review(db, draft_id, request.session_key, request.review_ticket, context.mailbox_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/agent/{conversation_id}/audit")
async def agent_audit(conversation_id: int,
                      session_key: str = Query(min_length=16, max_length=255),
                      db: Session = Depends(get_db),
                      context: MailboxContext = Depends(require_mailbox_context)):
    conversation = db.get(AgentConversation, conversation_id)
    if (not conversation or conversation.session_key != session_key
            or conversation.mailbox_id != context.mailbox_id):
        raise HTTPException(status_code=404, detail="Conversation was not found")
    rows = db.scalars(select(AgentAudit).where(
        AgentAudit.conversation_id == conversation_id
    ).order_by(AgentAudit.step)).all()
    return {"conversation_id": conversation_id, "steps": [
        {"step": row.step, "event": row.event, "tool": row.tool_name,
         "reason": row.summary, "details": row.details_json} for row in rows
    ]}


@router.get("/agent/{conversation_id}/incident")
async def agent_incident(conversation_id: int,
                         session_key: str = Query(min_length=16, max_length=255),
                         db: Session = Depends(get_db),
                         context: MailboxContext = Depends(require_mailbox_context)):
    """Redacted support timeline: no transcript, body, recipient, subject, or tool payload."""
    conversation = db.get(AgentConversation, conversation_id)
    if (not conversation or conversation.session_key != session_key
            or conversation.mailbox_id != context.mailbox_id):
        raise HTTPException(status_code=404, detail="Conversation was not found")
    rows = db.scalars(select(AgentAudit).where(
        AgentAudit.conversation_id == conversation_id).order_by(AgentAudit.step)).all()
    settings = get_settings()
    return {"conversation_id": conversation_id,
            "created_at": conversation.created_at,
            "completed": bool(conversation.assistant_message),
            "text_model": settings.gemini_model,
            "live_model": settings.gemini_live_model,
            "events": [{"step": row.step, "event": row.event,
                        "tool": row.tool_name, "created_at": row.created_at}
                       for row in rows]}


@router.get("/pending-actions")
async def pending_actions(db: Session = Depends(get_db),
                          context: MailboxContext = Depends(require_mailbox_context)):
    rows = db.scalars(select(AgentAction).join(AgentConversation).where(
        AgentConversation.mailbox_id == context.mailbox_id,
        AgentAction.status == "PENDING_APPROVAL"
    ).order_by(AgentAction.created_at.desc())).all()
    return {"actions": [{"id": row.id, "type": row.action_type,
                          "arguments": row.arguments_json, "reason": row.rationale,
                          "status": row.status} for row in rows]}


@router.get("/app", include_in_schema=False)
async def phone_app():
    apk = PROJECT_ROOT / "dist" / "morning-mail.apk"
    if not apk.exists():
        raise HTTPException(status_code=404, detail="Android app is not built")
    return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="morning-mail.apk")


@router.get("/local-ca", include_in_schema=False)
async def local_ca():
    certificate = PROJECT_ROOT / "secrets" / "local-ca.cer"
    if not certificate.exists():
        raise HTTPException(404, "Local HTTPS has not been configured")
    return FileResponse(certificate, media_type="application/pkix-cert", filename="morning-mail-local-ca.cer")


@router.get("/preview", include_in_schema=False)
async def phone_preview(request: Request):
    settings = get_settings()
    code = request.query_params.get("code", "")
    valid_code = bool(settings.phone_install_code and hmac.compare_digest(code, settings.phone_install_code))
    cookie = request.cookies.get("morning_mail_phone", "")
    paired_cookie = bool(settings.phone_install_code and hmac.compare_digest(
        cookie, phone_session_token(settings.phone_install_code)))
    if valid_code:
        response = RedirectResponse("/phone/preview", status_code=303)
        response.set_cookie("morning_mail_phone", phone_session_token(settings.phone_install_code),
                            max_age=30 * 24 * 60 * 60, secure=True, httponly=True,
                            samesite="strict", path="/phone")
        return response
    client_host = request.client.host if request.client else ""
    if paired_cookie or (request.url.hostname in LOOPBACK_NAMES and client_host in LOOPBACK_NAMES):
        return FileResponse(PROJECT_ROOT / "app" / "static" / "phone-preview.html")
    error = "That code was not accepted. Check it and try again." if code else "Enter the pairing code supplied with your phone link."
    return HTMLResponse(f'''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair Morning Mail</title><main style="font:16px system-ui;max-width:28rem;margin:12vh auto;padding:1.5rem">
<h1>Connect Morning Mail</h1><p>{error}</p><form action="/phone/preview" method="get">
<label for="code">Pairing code</label><p><input id="code" name="code" type="password" inputmode="numeric" autocomplete="one-time-code" required></p>
<button type="submit">Connect phone</button></form></main></html>''')


@router.get("/briefing")
async def phone_briefing(db: Session = Depends(get_db)):
    settings = get_settings()
    today = datetime.now(timezone.utc).astimezone(settings.timezone).date()
    briefing = db.scalar(
        select(Briefing)
        .join(BriefingRun, BriefingRun.briefing_id == Briefing.id)
        .where(
            BriefingRun.provider == settings.email_provider,
            BriefingRun.status == "COMPLETE",
        )
        .order_by(Briefing.generated_at.desc())
        .limit(1)
    )
    if not briefing:
        raise HTTPException(status_code=404, detail="No saved briefing exists yet")
    rows = db.execute(
        select(BriefingItem, EmailAnalysis, Email)
        .join(EmailAnalysis, EmailAnalysis.id == BriefingItem.email_analysis_id)
        .join(Email, Email.id == EmailAnalysis.email_id)
        .where(BriefingItem.briefing_id == briefing.id)
        .order_by(BriefingItem.rank)
    ).all()
    degraded = any("DEGRADED_RULES_ONLY" in (analysis.risk_flags_json or [])
                   for _item, analysis, _email in rows)
    spoken_brief = briefing.spoken_brief
    if degraded:
        spoken_brief = "Gemini was unavailable, so this is a rules-based brief. " + spoken_brief
    return {
        "briefing_date": briefing.briefing_date,
        "generated_at": briefing.generated_at.replace(tzinfo=timezone.utc).astimezone(settings.timezone),
        "total_emails": briefing.total_emails,
        "critical_count": briefing.critical_count,
        "high_count": briefing.high_count,
        "reply_count": briefing.reply_count,
        "action_count": briefing.action_count,
        "detailed_brief": briefing.detailed_brief,
        "spoken_brief": spoken_brief,
        "degraded": degraded,
        "items": [
            {
                "rank": item.rank,
                "message_id": email.provider_message_id,
                "thread_id": email.thread_id,
                "section": "Still Needs Attention" if item.section == "Outstanding Follow-ups" else item.section,
                "status": item.user_status,
                "importance": analysis.importance,
                "sender": analysis.sender,
                "subject": analysis.subject,
                "summary": analysis.summary,
                "why_important": analysis.why_important,
                "requested_action": analysis.requested_action,
                "deadline": analysis.deadline,
                "needs_reply": analysis.needs_reply,
                "needs_action": analysis.needs_action,
            }
            for item, analysis, email in rows
        ],
    }


@router.get("/recent")
async def phone_recent_mail(limit: int = Query(default=20, ge=1, le=50),
                            db: Session = Depends(get_db),
                            context: MailboxContext = Depends(require_mailbox_context)):
    """Show recently synced messages, including messages outside the newest sync window."""
    settings = get_settings()
    latest_analysis_id = (select(EmailAnalysis.id)
                          .where(EmailAnalysis.email_id == Email.id)
                          .order_by(EmailAnalysis.created_at.desc(), EmailAnalysis.id.desc())
                          .limit(1).correlate(Email).scalar_subquery())
    rows = db.execute(select(Email, EmailAnalysis)
                      .outerjoin(EmailAnalysis, EmailAnalysis.id == latest_analysis_id)
                      .where(Email.provider == context.provider,
                             Email.mailbox_id == context.mailbox_id, Email.is_deleted.is_(False))
                      .order_by(Email.received_at.desc(), Email.id.desc())
                      .limit(limit)).all()
    messages = []
    for email, analysis in rows:
        messages.append({
            "message_id": email.provider_message_id,
            "thread_id": email.thread_id,
            "received_at": email.received_at.replace(tzinfo=timezone.utc).astimezone(settings.timezone),
            "sender": (analysis.sender if analysis else "") or email.sender_name or "Unknown sender",
            "subject": (analysis.subject if analysis else "") or email.subject or "(no subject)",
            "summary": analysis.summary if analysis else "Message synced; analysis is not available yet.",
            "importance": analysis.importance if analysis else "UNANALYZED",
            "degraded": bool(analysis and "DEGRADED_RULES_ONLY" in (analysis.risk_flags_json or [])),
        })
    return {"count": len(messages), "emails": messages}


@router.get("/commitments")
async def phone_commitments(db: Session = Depends(get_db),
                            context: MailboxContext = Depends(require_mailbox_context)):
    settings = get_settings()
    rows = db.execute(select(Commitment, Email).join(Email).where(
        Email.mailbox_id == context.mailbox_id,
        Email.provider == context.provider, Email.is_deleted.is_(False),
        Commitment.status == "OPEN").order_by(Email.received_at.desc()).limit(20)).all()
    items = [{"id": row.id, "message_id": email.provider_message_id,
              "thread_id": email.thread_id, "sender": email.sender_name,
              "subject": email.subject, "direction": row.direction,
              "commitment": row.source_text, "due": row.due_text}
             for row, email in rows]
    yours = sum(item["direction"] == "YOU_OWE" for item in items)
    theirs = len(items) - yours
    return {"count": len(items), "you_owe": yours, "they_owe": theirs,
            "spoken_answer": f"You owe {yours}. Others owe you {theirs}.", "commitments": items}


@router.get("/waiting")
async def phone_waiting(days: int = Query(default=3, ge=1, le=30),
                        service=Depends(get_generation_service)):
    """Sent threads with no later reply; read-only and evidence-linked."""
    provider = service.provider
    if not hasattr(provider, "unanswered"):
        return {"count": 0, "days": days, "spoken_answer": "Waiting-on radar needs Gmail mode.", "threads": []}
    try:
        rows = await run_in_threadpool(provider.unanswered, days, 20)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Waiting-on radar could not reach Gmail.") from exc
    threads = [{**row, "safe_actions": ["Review", "Draft follow-up", "Defer"]} for row in rows]
    spoken = (f"{len(threads)} conversations are waiting for a reply."
              if threads else f"No sent conversations are waiting beyond {days} days.")
    return {"count": len(threads), "days": days, "spoken_answer": spoken, "threads": threads}


@router.get("/resume")
async def phone_resume(session_key: str = Query(min_length=16, max_length=255),
                       db: Session = Depends(get_db),
                       context: MailboxContext = Depends(require_mailbox_context)):
    """Recover persisted work after voice/network loss without replaying an action."""
    recover_stale_sends(db, mailbox_id=context.mailbox_id)
    conversation = db.scalar(select(AgentConversation).where(
        AgentConversation.mailbox_id == context.mailbox_id,
        AgentConversation.session_key == session_key
    ).order_by(AgentConversation.id.desc()).limit(1))
    if not conversation:
        return {"state": "EMPTY", "answer": "No interrupted work is waiting.", "evidence": []}
    draft = db.scalar(select(AgentAction).join(
        AgentConversation, AgentConversation.id == AgentAction.conversation_id
    ).where(AgentConversation.session_key == session_key,
            AgentConversation.mailbox_id == context.mailbox_id,
            AgentAction.action_type == "PREPARE_DRAFT",
            AgentAction.status == "PREPARED").order_by(AgentAction.id.desc()).limit(1))
    evidence = {}
    for audit in db.scalars(select(AgentAudit).where(
        AgentAudit.conversation_id == conversation.id,
        AgentAudit.event == "TOOL").order_by(AgentAudit.step)).all():
        for item in audit.details_json.get("result", {}).get("messages", []):
            if item.get("message_id"):
                evidence[item["message_id"]] = {key: item.get(key, "") for key in
                                                ("message_id", "thread_id", "sender", "subject", "received_at")}
    answer = conversation.assistant_message.strip()
    state = "RECOVERED" if answer else "INTERRUPTED"
    if not answer:
        answer = "Your last request did not finish. Type it again to continue safely."
    result = {"state": state, "answer": answer, "spoken_answer": answer,
              "evidence": list(evidence.values())}
    if draft:
        result["draft"] = {"id": draft.id, **{key: draft.arguments_json.get(key, "")
                                               for key in ("to", "subject", "body", "thread_id")}}
    uncertain = db.scalar(select(AgentAction).join(
        AgentConversation, AgentConversation.id == AgentAction.conversation_id
    ).where(AgentConversation.session_key == session_key,
            AgentConversation.mailbox_id == context.mailbox_id,
            AgentAction.action_type == "PREPARE_DRAFT",
            AgentAction.status.in_(("SENDING", "SEND_UNCONFIRMED")))
        .order_by(AgentAction.id.desc()).limit(1))
    if uncertain:
        warning = "I cannot confirm whether this email was sent. Check Sent in the connected mailbox before any new attempt."
        result["send_outcome_unknown"] = {"draft_id": uncertain.id, "message": warning}
        if uncertain.conversation_id == conversation.id:
            result.update(state="SEND_UNCONFIRMED", answer=warning, spoken_answer=warning)
    return result
