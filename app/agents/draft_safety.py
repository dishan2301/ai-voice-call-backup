"""Application-owned draft review; no model tool can acknowledge a read-back."""
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import or_, select, update

from app.core.config import get_settings
from app.database.models import AgentAction, AgentConversation, DraftReview


CONFIRMATIONS = {"done", "send it", "bhej do"}
SEND_RECOVERY_DELAY = timedelta(minutes=2)
BUSINESS_STATE_WORDS = {
    "approved", "booked", "completed", "confirmed", "delivered", "dispatched",
    "fixed", "included", "paid", "ready", "received", "resolved", "scheduled",
    "shipped", "tested", "working", "available", "compatible", "warranty",
}


def fingerprint(arguments):
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def recover_stale_sends(db, now=None, mailbox_id=None):
    """Keep a lost provider response as unknown; never put the draft back in PREPARED."""
    cutoff = (now or datetime.utcnow()) - SEND_RECOVERY_DELAY
    conditions = [
        AgentAction.action_type == "PREPARE_DRAFT",
        AgentAction.status == "SENDING",
        or_(AgentAction.approved_at < cutoff, AgentAction.approved_at.is_(None)),
    ]
    if mailbox_id is not None:
        conditions.append(AgentAction.conversation_id.in_(select(AgentConversation.id).where(
            AgentConversation.mailbox_id == mailbox_id)))
    count = db.execute(update(AgentAction).where(*conditions).values(
        status="SEND_UNCONFIRMED")).rowcount
    if count:
        db.commit()
    return count


def exact_readback(arguments):
    return f"To: {arguments['to']}\nSubject: {arguments['subject']}\n\n{arguments['body']}"


def owned_draft(db, draft_id, session_key, mailbox_id=None):
    mailbox_id = get_settings().mailbox_id if mailbox_id is None else mailbox_id
    row = db.scalar(select(AgentAction).join(AgentConversation).where(
        AgentAction.id == draft_id, AgentConversation.session_key == session_key,
        AgentConversation.mailbox_id == mailbox_id,
        AgentAction.action_type == "PREPARE_DRAFT", AgentAction.status == "PREPARED",
    ))
    if row is None:
        raise ValueError("That draft is not available in this conversation.")
    return row


def begin_review(db, draft_id, session_key, mailbox_id=None):
    draft = owned_draft(db, draft_id, session_key, mailbox_id)
    ticket = secrets.token_urlsafe(32)
    review = db.get(DraftReview, draft_id) or DraftReview(draft_id=draft_id)
    review.session_key = session_key
    review.fingerprint = fingerprint(draft.arguments_json)
    review.ticket_hash = hashlib.sha256(ticket.encode()).hexdigest()
    review.started_at = datetime.utcnow()
    review.completed_at = None
    db.add(review)
    db.commit()
    return {"draft_id": draft.id, "review_ticket": ticket,
            "readback": exact_readback(draft.arguments_json)}


def complete_review(db, draft_id, session_key, ticket, mailbox_id=None):
    draft = owned_draft(db, draft_id, session_key, mailbox_id)
    review = db.get(DraftReview, draft_id)
    if (not review or review.session_key != session_key
            or not hmac.compare_digest(review.ticket_hash, hashlib.sha256(ticket.encode()).hexdigest())
            or review.fingerprint != fingerprint(draft.arguments_json)
            or datetime.utcnow() - review.started_at > timedelta(minutes=10)):
        raise ValueError("The draft changed or its review expired. Read it again.")
    # Replaying an acknowledgement must not extend the approval window.
    if review.completed_at is None:
        review.completed_at = datetime.utcnow()
        db.commit()
    return {"state": "CONFIRMING", "draft_id": draft_id, "expires_in_seconds": max(
        0, 60 - int((datetime.utcnow() - review.completed_at).total_seconds()))}


def reviewed(db, draft, session_key, ticket, mailbox_id=None):
    try:
        owned_draft(db, draft.id, session_key, mailbox_id)
    except ValueError:
        return False
    review = db.get(DraftReview, draft.id)
    return bool(review and review.completed_at and review.session_key == session_key
                and hmac.compare_digest(review.ticket_hash, hashlib.sha256(ticket.encode()).hexdigest())
                and review.fingerprint == fingerprint(draft.arguments_json)
                and datetime.utcnow() - review.completed_at <= timedelta(seconds=60))


def validate_draft(arguments, message, evidence):
    """Bind replies to provider evidence; reject unverified headers and numeric facts."""
    if re.fullmatch(r"(?:please )?(?:reply|respond)(?: to)? (?:that |the |this )?(?:customer|sender|email|mail|them|unko|usse|unhe|usko)[.! ]*",
                    message.strip(), re.I):
        raise ValueError("What should the reply say: acknowledge the message, or include your own details?")
    draft = {field: str(arguments.get(field, "")) for field in
             ("to", "subject", "body", "thread_id")}
    draft["to"] = parseaddr(draft["to"])[1] or draft["to"].strip()
    if not re.fullmatch(r"[^\s@,<>;]+@[^\s@,<>;]+\.[^\s@,<>;]+", draft["to"]):
        raise ValueError("Choose one recipient address before drafting.")
    if not draft["subject"].strip() or any(c in draft["subject"] for c in "\r\n") or not draft["body"].strip():
        raise ValueError("A subject and reply text are required.")
    if len(draft["subject"]) > 998 or len(draft["body"]) > 20000:
        raise ValueError("This draft is too long; shorten it first.")
    explicit = {a.lower() for a in re.findall(r"[^\s@,<>;]+@[^\s@,<>;]+\.[^\s@,<>;.!?]+", message)}
    if draft["thread_id"]:
        rows = [e for e in evidence if e.get("thread_id") == draft["thread_id"]]
        if not rows:
            raise ValueError("Select and read the source thread before drafting a reply.")
        addresses = {parseaddr(e.get("sender", ""))[1].lower() for e in rows}
        if draft["to"].lower() not in addresses | explicit:
            raise ValueError("That recipient is not in the source thread. Name the recipient explicitly.")
        source = rows[-1]
        message_id = str(source.get("rfc_message_id", "")).strip()
        if message_id and not any(c in message_id for c in "\r\n"):
            draft["in_reply_to"] = message_id
            references = str(source.get("references", "")).strip()
            if not any(c in references for c in "\r\n"):
                reference_ids = references.split()
                if message_id not in reference_ids:
                    reference_ids.append(message_id)
                draft["references"] = " ".join(reference_ids)
    elif draft["to"].lower() not in explicit:
        raise ValueError("Name the recipient explicitly or select a source thread.")
    source_text = message + " " + " ".join(str(e.get("body", "")) + " " + str(e.get("subject", ""))
                                           for e in evidence)
    claims = re.findall(r"\b\d[\w./:%-]*\b", draft["body"])
    if any(c.lower() not in source_text.lower() for c in claims):
        raise ValueError("The reply contains an unverified number or date. Supply the exact facts first.")
    dates = re.findall(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|yesterday|january|february|march|april|may|june|july|august|september|october|november|december)\b", draft["body"], re.I)
    if any(not re.search(r"\b" + re.escape(day) + r"\b", source_text, re.I) for day in dates):
        raise ValueError("The reply introduces an unverified date. Supply the date first.")
    if re.search(r"\b(?:we|i) (?:will|shall|promise|guarantee)\b", draft["body"], re.I) and not re.search(
            r"\b(?:will|shall|promise|guarantee|commit)\b", message, re.I):
        raise ValueError("Confirm the commitment in your own words before it is added to the reply.")
    unsupported_states = sorted({word for word in re.findall(r"\b[a-z]+\b", draft["body"].lower())
                                 if word in BUSINESS_STATE_WORDS and
                                 not re.search(r"\b" + re.escape(word) + r"\b", source_text, re.I)})
    if unsupported_states:
        raise ValueError("The reply introduces an unverified business status. Supply that fact explicitly first.")
    return draft
