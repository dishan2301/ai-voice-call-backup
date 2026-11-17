from datetime import datetime
from typing import Iterable, List, Tuple

from app.schemas.analysis import Analysis, Importance, Urgency
from app.schemas.briefing import RankedAnalysis


IMPORTANCE_SCORE = {
    Importance.CRITICAL: 5,
    Importance.HIGH: 4,
    Importance.MEDIUM: 3,
    Importance.LOW: 2,
    Importance.IGNORE: 1,
}
URGENCY_SCORE = {
    Urgency.IMMEDIATE: 5,
    Urgency.TODAY: 4,
    Urgency.THIS_WEEK: 3,
    Urgency.LATER: 2,
    Urgency.NONE: 1,
}


def section_for(analysis: Analysis) -> str:
    if analysis.importance == Importance.CRITICAL or (
        analysis.importance == Importance.HIGH and analysis.urgency == Urgency.IMMEDIATE
    ):
        return "Immediate Attention"
    if analysis.needs_reply:
        return "Replies Needed"
    if analysis.needs_action:
        return "Action Needed, No Reply"
    if analysis.deadline or analysis.category.value == "MEETING":
        return "Meetings / Deadlines"
    if analysis.importance in {Importance.MEDIUM, Importance.HIGH}:
        return "Useful Updates"
    return "Low Priority Summary"


def rank_analyses(items: Iterable[Tuple[Analysis, datetime]]) -> List[RankedAnalysis]:
    ordered = sorted(
        items,
        key=lambda item: (
            IMPORTANCE_SCORE[item[0].importance],
            URGENCY_SCORE[item[0].urgency],
            item[0].needs_reply,
            item[0].needs_action,
            item[1],
        ),
        reverse=True,
    )
    return [
        RankedAnalysis(rank=index, section=section_for(analysis), analysis=analysis)
        for index, (analysis, _) in enumerate(ordered, start=1)
    ]
