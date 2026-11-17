from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.desktop.morning_prompt import PromptDecision
from app.services.morning_job import MorningJob, _build_job


router = APIRouter(prefix="/briefings", tags=["briefing decisions"])


class DecisionRequest(BaseModel):
    decision: PromptDecision


async def get_morning_job() -> MorningJob:
    return _build_job()


@router.post("/{briefing_id}/decision")
async def briefing_decision(
    briefing_id: int,
    request: DecisionRequest,
    job: MorningJob = Depends(get_morning_job),
):
    try:
        job.handle_decision(briefing_id, request.decision, datetime.now(timezone.utc))
        return {"briefing_id": briefing_id, "decision": request.decision}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
