from datetime import datetime, timedelta, timezone
from typing import Optional

from app.agents.briefing_agent import build_briefing
from app.agents.email_analyzer import DemoAnalyzer
from app.agents.ranking import rank_analyses
from app.core.config import Settings, get_settings
from app.email.base import EmailProvider
from app.email.demo import DemoEmailProvider
from app.email.thread_builder import build_threads
from app.schemas.briefing import BriefingDocument


class BriefingService:
    def __init__(self, provider: EmailProvider, settings: Optional[Settings] = None):
        self.provider = provider
        self.settings = settings or get_settings()
        self.analyzer = DemoAnalyzer()

    def generate(self, now: Optional[datetime] = None) -> BriefingDocument:
        period_end = now or datetime.now(timezone.utc)
        period_start = period_end - timedelta(hours=self.settings.initial_lookback_hours)
        messages = self.provider.fetch_messages(period_start, period_end)
        cards = build_threads(messages)
        analyzed = [(self.analyzer.analyze(card), card.newest.received_at) for card in cards]
        ranked = rank_analyses(analyzed)
        local_date = period_end.astimezone(self.settings.timezone).date() if hasattr(self.settings, "timezone") else period_end.date()
        return build_briefing(
            ranked,
            briefing_date=local_date,
            period_start=period_start,
            period_end=period_end,
        )


def demo_briefing(now: Optional[datetime] = None) -> BriefingDocument:
    moment = now or datetime.now(timezone.utc)
    return BriefingService(DemoEmailProvider(now=moment)).generate(now=moment)
