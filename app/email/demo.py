import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from app.email.base import EmailProvider
from app.schemas.mail import MailMessage


DEFAULT_FIXTURE = Path(__file__).with_name("demo_emails.json")


class DemoEmailProvider(EmailProvider):
    def __init__(self, fixture_path: Path = DEFAULT_FIXTURE, now: Optional[datetime] = None):
        self.fixture_path = fixture_path
        self.now = now or datetime.now(timezone.utc)

    def fetch_messages(self, since: datetime, until: datetime) -> List[MailMessage]:
        records = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        messages: List[MailMessage] = []
        for record in records:
            hours_ago = float(record.pop("received_hours_ago"))
            message = MailMessage(
                **record,
                received_at=self.now - timedelta(hours=hours_ago),
            )
            if since <= message.received_at < until:
                messages.append(message)
        return messages
