from datetime import date, datetime
from typing import Iterable, List

from app.schemas.analysis import Importance
from app.schemas.briefing import BriefingDocument, RankedAnalysis


def _detail_item(item: RankedAnalysis) -> str:
    analysis = item.analysis
    reply = "Yes" if analysis.needs_reply else "No"
    deadline = analysis.deadline or "None stated"
    return (
        f"### {item.rank}. {analysis.sender} — {analysis.subject}\n\n"
        f"{analysis.summary}\n\n"
        f"**Why it matters:** {analysis.why_important}\n\n"
        f"**Reply needed:** {reply}  \n"
        f"**Recommended next step:** {analysis.requested_action}  \n"
        f"**Deadline:** {deadline}"
    )


def build_briefing(
    ranked: Iterable[RankedAnalysis],
    briefing_date: date,
    period_start: datetime,
    period_end: datetime,
) -> BriefingDocument:
    items = list(ranked)
    current_items = [item for item in items if item.section != "Still Needs Attention"]
    analyses = [item.analysis for item in current_items]
    critical_count = sum(item.importance == Importance.CRITICAL for item in analyses)
    high_count = sum(item.importance == Importance.HIGH for item in analyses)
    reply_count = sum(item.needs_reply for item in analyses)
    action_count = sum(item.needs_action and not item.needs_reply for item in analyses)
    low_count = sum(item.importance in {Importance.LOW, Importance.IGNORE} for item in analyses)

    attention_count = sum(
        item.importance in {Importance.CRITICAL, Importance.HIGH, Importance.MEDIUM}
        or item.needs_reply
        or item.needs_action
        or bool(item.deadline)
        for item in analyses
    )
    status_line = (
        "There are no new emails since your previous briefing, so there are no new important emails."
        if not current_items
        else "There are no new important emails or actions."
        if not attention_count
        else f"{attention_count} new items need your attention."
    )
    header = (
        f"# Mail Brief — {briefing_date.isoformat()}\n\n"
        f"I reviewed {len(current_items)} new email threads. {status_line}\n\n"
        f"- {critical_count} critical items\n"
        f"- {high_count} high-priority items\n"
        f"- {reply_count} emails that need replies\n"
        f"- {action_count} other actions\n"
        f"- {low_count} low-priority or informational emails"
    )
    sections: List[str] = [header]
    for section_name in (
        "Immediate Attention",
        "Replies Needed",
        "Action Needed, No Reply",
        "Meetings / Deadlines",
        "Useful Updates",
        "Still Needs Attention",
    ):
        section_items = [item for item in items if item.section == section_name]
        if section_items:
            sections.append(f"## {section_name}\n\n" + "\n\n".join(_detail_item(item) for item in section_items))
    sections.append(
        f"## Low Priority Summary\n\nThere were {low_count} informational or low-priority emails. "
        "They were not included in the spoken attention list."
    )
    detailed = "\n\n".join(sections)

    spoken_items = [
        item
        for item in items
        if item.analysis.importance in {Importance.CRITICAL, Importance.HIGH, Importance.MEDIUM}
        or item.analysis.needs_reply
        or item.analysis.needs_action
        or item.analysis.deadline
    ]
    current_attention = [item for item in spoken_items if item.section != "Still Needs Attention"]
    outstanding = [item for item in spoken_items if item.section == "Still Needs Attention"]
    if not current_items:
        opening = (
            "There are no new emails since your previous briefing, "
            "so there are no new important emails."
        )
    elif not current_attention:
        opening = (
            f"I reviewed {len(current_items)} new email threads. "
            "There are no new important emails or actions."
        )
    else:
        opening = (
            f"I reviewed {len(current_items)} new email threads. "
            f"{len(current_attention)} need your attention."
        )
    if outstanding:
        opening += f" You also have {len(outstanding)} outstanding important items."
    spoken: List[str] = [opening]
    # A busy listener gets the executive cut: the three highest-ranked actions.
    # The screen keeps every item and is the place for supporting detail.
    for index, item in enumerate(spoken_items[:3], start=1):
        analysis = item.analysis
        deadline = f" Deadline: {analysis.deadline}." if analysis.deadline else ""
        spoken.append(f"{index}. {analysis.sender}: {analysis.summary}{deadline}")
    remaining = len(spoken_items) - 3
    if remaining > 0:
        spoken.append(f"{remaining} more items are ready on screen.")

    return BriefingDocument(
        briefing_date=briefing_date,
        period_start=period_start,
        period_end=period_end,
        total_emails=len(current_items),
        critical_count=critical_count,
        high_count=high_count,
        reply_count=reply_count,
        action_count=action_count,
        low_priority_count=low_count,
        detailed_brief=detailed,
        spoken_brief=" ".join(spoken),
        items=items,
    )
