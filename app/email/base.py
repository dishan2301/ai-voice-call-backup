from abc import ABC, abstractmethod
from datetime import datetime
from typing import List

from app.schemas.mail import MailMessage


class EmailProvider(ABC):
    """Read-only source of canonical email records."""

    @abstractmethod
    def fetch_messages(self, since: datetime, until: datetime) -> List[MailMessage]:
        """Return messages received inside the requested half-open time window."""
        raise NotImplementedError
