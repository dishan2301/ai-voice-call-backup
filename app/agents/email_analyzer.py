import re
from typing import List

from app.schemas.analysis import Analysis, Category, Importance, Urgency
from app.schemas.mail import ThreadCard


class DemoAnalyzer:
    """Deterministic demo analyzer; live providers are added in Phase 4."""

    def __init__(
        self,
        vip_senders=None,
        vip_domains=None,
        important_keywords=None,
        enabled_categories=None,
    ):
        self.vip_senders = {value.lower() for value in (vip_senders or [])}
        self.vip_domains = {value.lower() for value in (vip_domains or [])}
        self.important_keywords = [value.lower() for value in (important_keywords or [])]
        self.enabled_categories = set(enabled_categories or [category.value for category in Category])

    def analyze(self, card: ThreadCard) -> Analysis:
        mail = card.newest
        text = f"{mail.sender_name}\n{mail.sender_email}\n{mail.subject}\n{mail.body_text}".lower()
        promotional = any(
            signal in text or signal in mail.labels
            for signal in ("unsubscribe", "90% off", "buy now", "CATEGORY_PROMOTIONS")
        )
        injection = bool(card.risk_flags)
        no_action = any(phrase in text for phrase in ("no action is required", "no further action", "for your information", "fyi only", "do not reply"))
        thanks_only = len(mail.body_text.split()) <= 12 and any(word in text for word in ("thanks", "thank you"))

        category = self._category(text, promotional)
        if category.value not in self.enabled_categories:
            category = Category.UNKNOWN
        needs_reply = not (promotional or no_action or thanks_only or injection) and (
            "?" in text
            or any(phrase in text for phrase in ("please confirm", "please choose", "please advise", "can you", "could you", "reply", "let me know", "update us", "your decision"))
        )
        needs_action = not (promotional or no_action or thanks_only or injection) and (
            needs_reply
            or category in {Category.PURCHASE_ORDER, Category.SECURITY, Category.LEGAL}
            or any(word in text for word in ("process", "review", "approve", "investigate"))
        )

        urgency = Urgency.NONE
        if not promotional and any(phrase in text for phrase in ("immediately", "system is unavailable", "service down")):
            urgency = Urgency.IMMEDIATE
        elif not promotional and any(phrase in text for phrase in ("today", "this morning", "before our meeting")):
            urgency = Urgency.TODAY
        elif not promotional and "this week" in text:
            urgency = Urgency.THIS_WEEK
        elif needs_action:
            urgency = Urgency.LATER

        importance = self._importance(category, urgency, needs_reply, needs_action, promotional, injection)
        sender_domain = mail.sender_email.rsplit("@", 1)[-1].lower()
        importance_signal = (
            mail.sender_email.lower() in self.vip_senders
            or sender_domain in self.vip_domains
            or any(keyword in text for keyword in self.important_keywords)
        )
        if importance_signal and not promotional and not injection:
            if importance == Importance.LOW:
                importance = Importance.MEDIUM
            elif importance == Importance.MEDIUM:
                importance = Importance.HIGH
        deadline = None if promotional or category == Category.NEWSLETTER else self._deadline(text)
        summary = self._summary(mail.body_text or mail.snippet)
        action = self._action(category, needs_reply, needs_action)
        why = self._why(category, importance, needs_reply)
        facts = self._facts(mail.body_text)

        return Analysis(
            importance=importance,
            category=category,
            needs_reply=needs_reply,
            needs_action=needs_action,
            urgency=urgency,
            sender=mail.sender_name or mail.sender_email,
            subject=mail.subject,
            summary=summary,
            why_important=why,
            requested_action=action,
            deadline=deadline,
            key_facts=facts,
            risk_flags=card.risk_flags,
            confidence=0.82 if not injection else 0.6,
        )

    @staticmethod
    def _category(text: str, promotional: bool) -> Category:
        if "newsletter" in text or "weekly digest" in text:
            return Category.NEWSLETTER
        if any(signal in text for signal in ("lottery winner", "advance fee", "phishing", "junk mail")):
            return Category.SPAM
        if promotional:
            return Category.MARKETING
        rules = (
            (Category.SECURITY, ("security warning", "unauthorized", "compromised", "service down", "system down", "system is unavailable")),
            (Category.LEGAL, ("legal notice", "legal action", "breach of contract", "court")),
            (Category.PURCHASE_ORDER, ("purchase order", " po-")),
            (Category.PAYMENT, ("payment", "paid")),
            (Category.INVOICE, ("invoice", "billing")),
            (Category.MEETING, ("meeting", "review at", "3 pm")),
            (Category.MANAGEMENT, ("manager", "management", "director", "your decision")),
            (Category.SALES, ("quotation", "quote", "proposal")),
            (Category.SUPPORT, ("support", "cannot check in", "investigate")),
            (Category.VENDOR, ("vendor", "supplier", "dispatch", "shipment")),
            (Category.RECRUITMENT, ("candidate", "interview", "recruitment", "resume")),
            (Category.PROJECT, ("project", "milestone", "sprint")),
            (Category.INTERNAL, ("internal notice", "team update", "office policy")),
            (Category.AUTOMATED, ("automated notification", "build completed", "do not reply")),
            (Category.ACCOUNT, ("account",)),
            (Category.CUSTOMER, ("customer", "client")),
            (Category.PERSONAL, ("family", "personal")),
        )
        for category, signals in rules:
            if any(re.search(r"(?<!\w)" + re.escape(signal) + r"(?!\w)", text) for signal in signals):
                return category
        return Category.UNKNOWN

    @staticmethod
    def _importance(category, urgency, needs_reply, needs_action, promotional, injection) -> Importance:
        if promotional or category == Category.SPAM:
            return Importance.IGNORE
        if injection:
            return Importance.LOW
        if category == Category.SECURITY and urgency == Urgency.IMMEDIATE:
            return Importance.CRITICAL
        if urgency in {Urgency.IMMEDIATE, Urgency.TODAY} or category in {
            Category.PURCHASE_ORDER,
            Category.LEGAL,
            Category.MANAGEMENT,
        }:
            return Importance.HIGH
        if needs_reply or needs_action or category in {Category.INVOICE, Category.PAYMENT}:
            return Importance.MEDIUM
        if category == Category.NEWSLETTER:
            return Importance.LOW
        return Importance.LOW

    @staticmethod
    def _deadline(text: str):
        for phrase in ("today at 3 pm", "today", "this week"):
            if phrase in text:
                return phrase.title()
        return None

    @staticmethod
    def _summary(body: str) -> str:
        compact = re.sub(r"\s+", " ", body).strip()
        if not compact:
            return "No message text was available for safe analysis."
        sentence = re.split(r"(?<=[.!?])\s+", compact, maxsplit=1)[0]
        return sentence[:320]

    @staticmethod
    def _action(category: Category, needs_reply: bool, needs_action: bool) -> str:
        if category == Category.PURCHASE_ORDER:
            return "Review and process the purchase order."
        if category == Category.SECURITY and needs_action:
            return "Investigate the incident and update the sender."
        if needs_reply:
            return "Review the request and reply to the sender."
        if needs_action:
            return "Review and complete the requested action."
        return "No action needed."

    @staticmethod
    def _why(category: Category, importance: Importance, needs_reply: bool) -> str:
        if importance == Importance.CRITICAL:
            return "Operations appear blocked and require immediate human attention."
        if category == Category.PURCHASE_ORDER:
            return "A customer order needs human processing."
        if needs_reply:
            return "The sender is waiting for a response or decision."
        return "This is informational or does not currently require action."

    @staticmethod
    def _facts(body: str) -> List[str]:
        facts: List[str] = []
        for match in re.findall(r"\b(?:PO-|INV-)?\d[\w-]*\b", body, flags=re.IGNORECASE):
            if match not in facts:
                facts.append(match)
        return facts[:5]
