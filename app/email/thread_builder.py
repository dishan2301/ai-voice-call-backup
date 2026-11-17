from hashlib import sha256
from typing import Dict, Iterable, List, Set, Tuple

from app.email.normalizer import injection_risk_flags, normalize_message
from app.schemas.mail import MailMessage, ThreadCard


def build_threads(messages: Iterable[MailMessage]) -> List[ThreadCard]:
    grouped: Dict[str, List[MailMessage]] = {}
    seen_ids: Set[str] = set()
    seen_content: Set[Tuple[str, str, str]] = set()

    for original in messages:
        message = normalize_message(original)
        content_hash = sha256(message.body_text.encode("utf-8")).hexdigest()
        duplicate_key = (message.sender_email.lower(), message.subject.lower(), content_hash)
        if message.message_id in seen_ids or duplicate_key in seen_content:
            continue
        seen_ids.add(message.message_id)
        seen_content.add(duplicate_key)
        grouped.setdefault(message.thread_id or message.message_id, []).append(message)

    cards: List[ThreadCard] = []
    for thread_id, thread_messages in grouped.items():
        ordered = sorted(thread_messages, key=lambda item: item.received_at)
        newest = ordered[-1]
        combined = "\n".join(item.body_text for item in ordered[-3:])
        cards.append(
            ThreadCard(
                thread_id=thread_id,
                newest=newest,
                context=ordered[-3:-1],
                body_hash=sha256(combined.encode("utf-8")).hexdigest(),
                risk_flags=injection_risk_flags(combined),
            )
        )
    return sorted(cards, key=lambda card: card.newest.received_at, reverse=True)
