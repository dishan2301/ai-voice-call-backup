from datetime import datetime, timedelta, timezone

from app.services.briefing_service import demo_briefing
from app.agents.briefing_agent import build_briefing
from app.schemas.analysis import Analysis
from app.schemas.briefing import RankedAnalysis


def test_demo_pipeline_generates_ranked_two_form_brief() -> None:
    result = demo_briefing(datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))

    assert result.total_emails == 8
    assert result.items[0].analysis.importance.value == "CRITICAL"
    assert result.reply_count >= 2
    assert result.low_priority_count >= 2
    assert len(result.spoken_brief) < len(result.detailed_brief)
    assert "90% OFF" not in result.spoken_brief
    assert "Buy now for a huge discount" not in result.spoken_brief
    assert "this week's articles" not in result.spoken_brief
    assert "PROMPT_INJECTION_ATTEMPT" in {
        flag for item in result.items for flag in item.analysis.risk_flags
    }


def test_demo_pipeline_preserves_business_identifiers() -> None:
    result = demo_briefing(datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))

    assert "PO-7841" in result.detailed_brief
    assert "INV-2048" in result.detailed_brief


def test_spoken_brief_is_an_executive_cut_with_three_items() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    items = [
        RankedAnalysis(
            rank=index,
            section="Replies Needed",
            analysis=Analysis(
                importance="HIGH",
                category="CUSTOMER",
                needs_reply=True,
                needs_action=True,
                urgency="TODAY",
                sender=f"Manager {index}",
                subject=f"Request {index}",
                summary=f"Important item {index}.",
                why_important="A manager needs an answer.",
                requested_action="Reply.",
                confidence=0.9,
            ),
        )
        for index in range(1, 11)
    ]

    brief = build_briefing(items, now.date(), now - timedelta(hours=1), now)

    assert "Important item 1" in brief.spoken_brief
    assert "Important item 3" in brief.spoken_brief
    assert "Important item 4" not in brief.spoken_brief
    assert "7 more items are ready on screen" in brief.spoken_brief
    assert len(brief.spoken_brief.split()) <= 70
