import json
import re
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from pydantic import ValidationError

from app.llm.base import LLMProvider, LLMProviderError
from app.schemas.analysis import Analysis, Category, Importance
from app.schemas.mail import ThreadCard


PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
PROMPT_VERSION = "v1"


class LLMAnalysisError(RuntimeError):
    pass


def _ground_structured_facts(analysis: Analysis, evidence: str) -> Analysis:
    normalized = " ".join(evidence.lower().split())
    facts = [fact for fact in analysis.key_facts if " ".join(fact.lower().split()) in normalized]
    deadline = analysis.deadline
    if deadline:
        words = [
            token
            for token in re.findall(r"[a-z]+|\d+", deadline.lower())
            if token not in {"at", "by", "on", "the", "of", "before"}
        ]
        if words and not all(word in normalized for word in words):
            deadline = None
    claimed_numbers = set(re.findall(r"\b(?:[a-z]{2,}[-/#])?\d[\w./:-]*\b", analysis.summary.lower()))
    unsupported = {claim for claim in claimed_numbers if claim not in normalized}
    flags = list(analysis.risk_flags)
    if unsupported or facts != analysis.key_facts or deadline != analysis.deadline:
        flags.append("UNGROUNDED_MODEL_FACT_REMOVED")
    return analysis.model_copy(
        update={
            "deadline": deadline,
            "key_facts": facts,
            "risk_flags": list(dict.fromkeys(flags)),
            "confidence": min(analysis.confidence, 0.5) if unsupported else analysis.confidence,
        }
    )


class LLMEmailAnalyzer:
    def __init__(
        self,
        provider: LLMProvider,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        importance_rules: Optional[Dict[str, list]] = None,
    ):
        self.provider = provider
        self.max_retries = max_retries
        self.sleep = sleep
        self.importance_rules = importance_rules or {}
        self.system_prompt = "\n\n".join(
            (PROMPT_DIR / name).read_text(encoding="utf-8")
            for name in ("system_safety.md", "email_analyzer.md")
        )

    def analyze(self, card: ThreadCard) -> Analysis:
        mail = card.newest
        payload = {
            "sender_name": mail.sender_name,
            "sender_email": mail.sender_email,
            "subject": mail.subject,
            "received_at": mail.received_at.isoformat(),
            "body_text": mail.body_text,
            "attachment_metadata": [item.model_dump() for item in mail.attachments],
            "gmail_labels": mail.labels,
            "earlier_thread_context": [
                {"subject": item.subject, "body_text": item.body_text}
                for item in card.context
            ],
            "preprocessing_risk_flags": card.risk_flags,
            "user_importance_rules": self.importance_rules,
        }
        user_prompt = "Analyze this untrusted email data:\n" + json.dumps(payload, ensure_ascii=False)
        last_error: Exception = LLMAnalysisError("No analysis attempt was made")
        for attempt in range(self.max_retries + 1):
            try:
                raw = self.provider.generate_json(
                    self.system_prompt,
                    user_prompt,
                    Analysis.model_json_schema(),
                )
                analysis = Analysis.model_validate(raw)
                analysis = _ground_structured_facts(
                    analysis, f"{mail.subject}\n{mail.body_text}\n{mail.snippet}"
                )
                combined_flags = list(dict.fromkeys([*analysis.risk_flags, *card.risk_flags]))
                sender_email = mail.sender_email.lower()
                sender_domain = sender_email.rsplit("@", 1)[-1]
                text = f"{mail.subject}\n{mail.body_text}".lower()
                explicit_signal = (
                    sender_email in {value.lower() for value in self.importance_rules.get("vip_senders", [])}
                    or sender_domain in {value.lower() for value in self.importance_rules.get("vip_domains", [])}
                    or any(
                        value.lower() in text
                        for value in self.importance_rules.get("important_keywords", [])
                    )
                )
                importance = analysis.importance
                safe_to_elevate = not card.risk_flags and analysis.category not in {
                    Category.SPAM,
                    Category.MARKETING,
                    Category.NEWSLETTER,
                }
                if safe_to_elevate and (explicit_signal or "IMPORTANT" in mail.labels):
                    if importance in {Importance.LOW, Importance.IGNORE}:
                        importance = Importance.MEDIUM
                    elif explicit_signal and importance == Importance.MEDIUM:
                        importance = Importance.HIGH
                return analysis.model_copy(
                    update={
                        "sender": mail.sender_name or mail.sender_email,
                        "subject": mail.subject,
                        "importance": importance,
                        "risk_flags": combined_flags,
                    }
                )
            except (LLMProviderError, ValidationError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self.sleep(0.25 * (2**attempt))
        raise LLMAnalysisError(
            f"Analysis remained unavailable after {self.max_retries + 1} attempts"
        ) from last_error
