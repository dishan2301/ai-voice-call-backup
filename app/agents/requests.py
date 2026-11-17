"""Shared input and authorization policy for HTTP and streaming mail requests."""
import secrets
import re

from app.agents.draft_safety import CONFIRMATIONS
from app.voice.normalizer import normalize_voice


def _starts_new_session(text: str) -> bool:
    """Recognize an explicit request to leave the current mail topic."""
    value = " ".join(str(text or "").casefold().split())
    return bool(
        re.search(
            r"\b(?:new|change|switch|start|reset|clear)\b[^?.!]{0,40}\b(?:session|conversation|mail topic|topic)\b"
            r"|\b(?:session|conversation|mail topic|topic)\b[^?.!]{0,40}\b(?:new|change|switch|start|reset|clear)\b",
            value,
        )
    )


def handle_mail_request(agent_factory, message, session_key, *, input_mode="text",
                        alternatives=None, preserve_literals=False, **options):
    if options.get("cancelled", lambda: False)():
        return {"cancelled": True}
    interpretation = normalize_voice(message, alternatives, input_mode, preserve_literals=preserve_literals)
    options.get("emit", lambda event: None)({
        "type": "transcript.final", "text": interpretation.text,
        "corrections": list(interpretation.corrections),
    })
    if _starts_new_session(interpretation.text):
        new_session_key = secrets.token_urlsafe(24)
        result = {
            "answer": "New mail conversation started. What would you like me to check?",
            "spoken_answer": "New mail conversation started. What should I check?",
            "state": "NEW_SESSION",
            "pending_question": "What would you like me to check?",
            "evidence": [],
            "session_key": new_session_key,
            "session_reset": True,
        }
    elif input_mode == "voice" and (
        message.strip().lower() in CONFIRMATIONS
        or interpretation.text.strip().lower() in CONFIRMATIONS):
        result = {"answer": "Type done or send it after the exact read-back to confirm this send.",
                  "spoken_answer": "Please confirm the send on screen after the exact read-back.",
                  "state": "CONFIRMING", "evidence": []}
    elif input_mode == "voice" and interpretation.tier in {"LOW", "UNUSABLE"}:
        answer = "I didn't catch that confidently. Could you repeat or type your request?"
        result = {"answer": answer, "spoken_answer": answer,
                  "state": "NEEDS_CLARIFICATION", "pending_question": answer,
                  "evidence": []}
    else:
        try:
            result = agent_factory().run(interpretation.text, session_key,
                                         confidence_tier=interpretation.tier, **options)
        except ValueError as exc:
            if "GEMINI_API_KEY is required" not in str(exc):
                raise
            answer = ("Gemini is not configured on this server yet. Add GEMINI_API_KEY, "
                      "then restart Morning Mail.")
            result = {"answer": answer,
                      "spoken_answer": "Gemini is not configured on this server yet.",
                      "state": "DEGRADED", "evidence": []}
    result.update(raw_transcript=message, interpretation=interpretation.text,
                  normalized_user_request=interpretation.text,
                  confidence_tier=interpretation.tier,
                  corrections=list(interpretation.corrections))
    result.setdefault("pending_question", None)
    return result
