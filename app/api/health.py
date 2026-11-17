import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.database.models import Briefing, BriefingItem, EmailAnalysis, SyncCheckpoint
from app.core.config import PROJECT_ROOT, get_settings
from app.services.database_backup import latest_backup, verify_backup
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(db: Session = Depends(get_db)) -> HealthResponse:
    db.execute(text("SELECT 1"))
    return HealthResponse(status="ok", database="ok")


@router.get("/health/ready")
async def readiness(db: Session = Depends(get_db)):
    """Local-only launch evidence; returns booleans and never credential values."""
    settings = get_settings()

    def exists(value: str) -> bool:
        path = Path(value)
        return (path if path.is_absolute() else PROJECT_ROOT / path).is_file()

    latest = db.scalar(select(Briefing.id).order_by(Briefing.generated_at.desc()).limit(1))
    latest_analysis = db.scalar(select(EmailAnalysis.risk_flags_json).order_by(
        EmailAnalysis.created_at.desc(), EmailAnalysis.id.desc()).limit(1))
    degraded = bool(latest_analysis and "DEGRADED_RULES_ONLY" in latest_analysis)
    backup = latest_backup()
    backup_verified = False
    try:
        if backup and 0 <= time.time() - backup.stat().st_mtime < 26 * 60 * 60:
            verify_backup(backup, settings)
            backup_verified = True
    except Exception:
        # Corrupt, unreadable or wrong-key backups cannot satisfy readiness.
        pass
    checkpoint = db.get(SyncCheckpoint, f"{settings.email_provider}:{settings.mailbox_id}")
    sync_age = ((datetime.utcnow() - checkpoint.last_successful_sync_at).total_seconds()
                if checkpoint and checkpoint.last_successful_sync_at else None)
    checks = {
        "supported_python": sys.version_info >= (3, 11),
        "gemini_key_configured": bool(settings.gemini_api_key),
        "gmail_read_token": exists(settings.google_token_file),
        "gmail_send_token": exists(settings.google_send_token_file),
        "https_certificate": (PROJECT_ROOT / "secrets/local-server.crt").is_file(),
        "saved_briefing": latest is not None,
        "recent_verified_backup": backup_verified,
        "latest_briefing_full_ai": bool(latest and latest_analysis) and not degraded,
        "production_environment": settings.app_env == "production",
        "mail_sync_checkpoint": bool(checkpoint and checkpoint.last_history_id),
        "mail_sync_fresh": bool(checkpoint and checkpoint.status == "ready" and sync_age is not None
                                and 0 <= sync_age <= settings.freshness_sla_minutes * 60),
        "mail_sync_status": checkpoint.status if checkpoint else "never",
        "mail_last_sync_at": checkpoint.last_successful_sync_at.isoformat() if checkpoint and checkpoint.last_successful_sync_at else None,
    }
    return {"status": "ready" if all(value for value in checks.values() if isinstance(value, bool)) else "needs_setup", "checks": checks,
            "text_model": settings.gemini_model, "live_model": settings.gemini_live_model}
