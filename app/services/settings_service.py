from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, get_settings
from app.database.db import SessionLocal
from app.database.models import Setting


def get_runtime_settings() -> Settings:
    """Merge validated dashboard preferences over environment configuration."""
    base = get_settings()
    try:
        with SessionLocal() as session:
            saved = {row.key: row.value_json for row in session.scalars(select(Setting)).all()}
    except SQLAlchemyError:
        return base
    updates = {
        key: saved[key]
        for key in (
            "briefing_time",
            "app_timezone",
            "tts_enabled",
            "tts_rate",
            "tts_volume",
            "vip_senders",
            "vip_domains",
            "important_keywords",
        )
        if key in saved
    }
    if saved.get("model"):
        updates["gemini_model"] = saved["model"]
    return base.model_copy(update=updates)
