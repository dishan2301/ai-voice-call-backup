#!/usr/bin/env python3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.email_analyzer import DemoAnalyzer
from app.core.config import get_settings
from app.database.db import create_tables
from app.email.demo import DemoEmailProvider
from app.services.persistent_briefing_service import PersistentBriefingService


if __name__ == "__main__":
    create_tables()
    now = datetime.now(timezone.utc)
    settings = get_settings().model_copy(update={"email_provider": "demo"})
    document = PersistentBriefingService(
        DemoEmailProvider(now=now), DemoAnalyzer(), settings
    ).generate(now)
    print(f"Stored demo briefing for {document.briefing_date} with {document.total_emails} threads.")
