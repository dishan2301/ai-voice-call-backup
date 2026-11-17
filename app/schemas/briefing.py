from datetime import date, datetime
from typing import List

from pydantic import BaseModel, ConfigDict

from app.schemas.analysis import Analysis


class RankedAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    section: str
    analysis: Analysis


class BriefingDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    briefing_date: date
    period_start: datetime
    period_end: datetime
    total_emails: int
    critical_count: int
    high_count: int
    reply_count: int
    action_count: int
    low_priority_count: int
    detailed_brief: str
    spoken_brief: str
    items: List[RankedAnalysis]


class SynthesizedBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detailed_brief: str
    spoken_brief: str
