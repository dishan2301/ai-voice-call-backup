from fastapi import APIRouter, Depends, HTTPException

from app.services.speech_service import SpeechService, tts_provider_dependency
from app.speech.base import TTSError, TTSProvider


router = APIRouter(tags=["speech"])


async def get_speech_service(
    provider: TTSProvider = Depends(tts_provider_dependency),
) -> SpeechService:
    return SpeechService(provider)


@router.post("/briefings/{briefing_id}/speak")
async def speak(briefing_id: int, service: SpeechService = Depends(get_speech_service)):
    try:
        return {"briefing_id": briefing_id, "status": service.speak(briefing_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TTSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/briefings/{briefing_id}/replay")
async def replay(briefing_id: int, service: SpeechService = Depends(get_speech_service)):
    return await speak(briefing_id, service)


@router.post("/speech/stop")
async def stop(service: SpeechService = Depends(get_speech_service)):
    try:
        service.stop()
        return {"status": "STOPPED"}
    except TTSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
