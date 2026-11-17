from functools import lru_cache
import os
from pathlib import Path
from typing import Dict, List
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATEGORIES = [
    "CUSTOMER", "SALES", "SUPPORT", "PURCHASE_ORDER", "PAYMENT", "INVOICE",
    "MEETING", "MANAGEMENT", "INTERNAL", "VENDOR", "SECURITY", "LEGAL",
    "ACCOUNT", "PROJECT", "RECRUITMENT", "NEWSLETTER", "MARKETING",
    "AUTOMATED", "SPAM", "PERSONAL", "UNKNOWN",
]


def _read_env_file(path: Path) -> Dict[str, str]:
    """Read a small dotenv subset without adding a runtime dependency."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _as_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    deployment_mode: Literal["personal", "hosted"] = "personal"
    allowed_hosts: List[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "::1", "test", "testserver"])
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8765, ge=1, le=65535)
    app_timezone: str = "Asia/Kolkata"
    briefing_time: str = "09:30"
    initial_lookback_hours: int = Field(default=24, ge=1, le=720)
    freshness_sla_minutes: int = Field(default=30, ge=1, le=1440)
    retrieval_adaptive_enabled: bool = False
    retrieval_backend: Literal["existing", "postgres"] = "existing"
    retrieval_database_url: str = Field(default="", repr=False)
    retrieval_content_enabled: bool = False
    retrieval_content_key_file: str = ""
    retrieval_semantic_enabled: bool = False
    retrieval_local_model_path: str = ""
    database_url: str = "sqlite:///./data/morning_mail_pa.db"
    database_key_file: str = "./secrets/database.key"
    provider_credential_key_directory: str = "./secrets/provider-credential-keys"
    provider_credential_key_version: int = Field(default=1, ge=1, le=999999)
    credential_key_directory: str = "./secrets/credential-keys"
    credential_active_key_version: str = "v1"
    microsoft_enabled: bool = False
    microsoft_client_id: str = ""
    microsoft_client_secret: str = Field(default="",repr=False)
    microsoft_redirect_uri: str = ""
    microsoft_allowed_emails: List[str] = Field(default_factory=list)
    email_provider: str = "demo"
    mailbox_id: str = "default"
    google_client_secret_file: str = "./secrets/google_client_secret.json"
    google_web_client_secret_file: str = "./secrets/google_web_client_secret.json"
    hosted_oauth_redirect_uri: str = ""
    hosted_allowed_emails: List[str] = Field(default_factory=list)
    google_token_file: str = "./secrets/google_token.json"
    google_send_token_file: str = "./secrets/google_send_token.json"
    google_calendar_token_file: str = "./secrets/google_calendar_token.json"
    google_calendar_redirect_uri: str = ""
    google_web_client_id: str = ""
    google_web_client_secret: str = ""
    google_web_redirect_uri: str = ""
    hosted_google_allowed_emails: List[str] = Field(default_factory=list)
    gmail_pubsub_topic: str = ""
    gemini_live_model: str = "gemini-3.8-live"
    gemini_live_fallback_models: List[str] = Field(default_factory=lambda: ["gemini-3.1-flash-live-preview"])
    voice_mode: Literal["gemini_live", "android_system"] = "gemini_live"
    voice_ice_urls: List[str] = Field(default_factory=list)
    voice_ice_username: str = ""
    voice_ice_credential: str = ""
    gemini_api_key: str = ""
    gemini_reply_api_key: str = ""
    gemini_reply_model: str = "gemini-3.5-flash-lite"
    gemini_model: str = "gemini-3.5-flash-lite"
    entity_match_high_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    entity_match_medium_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    entity_match_min_margin: float = Field(default=0.08, ge=0.0, le=1.0)
    phone_api_token: str = ""
    phone_install_code: str = ""
    tts_enabled: bool = True
    tts_provider: str = "system"
    tts_rate: int = Field(default=165, ge=50, le=400)
    tts_volume: float = Field(default=1.0, ge=0.0, le=1.0)
    log_level: str = "INFO"
    scheduler_enabled: bool = True
    vip_senders: List[str] = Field(default_factory=list)
    vip_domains: List[str] = Field(default_factory=list)
    important_keywords: List[str] = Field(default_factory=list)
    enabled_categories: List[str] = Field(default_factory=lambda: list(DEFAULT_CATEGORIES))

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("APP_TIMEZONE must be a valid IANA timezone") from exc
        return value

    @field_validator("mailbox_id")
    @classmethod
    def valid_mailbox_id(cls, value: str) -> str:
        value = str(value).strip()
        if not value or ":" in value or len(value) > 120:
            raise ValueError("MAILBOX_ID must be a non-empty identifier without ':'")
        return value

    @field_validator("voice_ice_urls")
    @classmethod
    def valid_voice_ice_urls(cls, values: List[str]) -> List[str]:
        for value in values:
            scheme = value.split(":", 1)[0].lower()
            if scheme not in {"stun", "stuns", "turn", "turns"} or "@" in value:
                raise ValueError("VOICE_ICE_URLS accepts only credential-free STUN/TURN URLs")
        return values

    @field_validator("briefing_time")
    @classmethod
    def valid_briefing_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("BRIEFING_TIME must use HH:MM")
        hour, minute = map(int, parts)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("BRIEFING_TIME must use a valid 24-hour time")
        return f"{hour:02d}:{minute:02d}"

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @classmethod
    def from_environment(cls) -> "Settings":
        file_values = _read_env_file(PROJECT_ROOT / ".env")
        raw = {**file_values, **os.environ}
        values = {
            "deployment_mode": raw.get("DEPLOYMENT_MODE", "personal"),
            "allowed_hosts": [host.strip().lower() for host in raw.get(
                "ALLOWED_HOSTS", "localhost,127.0.0.1,::1,test,testserver").split(",") if host.strip()],
            "app_env": raw.get("APP_ENV", "development"),
            "app_host": raw.get("APP_HOST", "127.0.0.1"),
            "app_port": raw.get("APP_PORT", 8765),
            "app_timezone": raw.get("APP_TIMEZONE", "Asia/Kolkata"),
            "briefing_time": raw.get("BRIEFING_TIME", "09:30"),
            "initial_lookback_hours": raw.get("INITIAL_LOOKBACK_HOURS", 24),
            "freshness_sla_minutes": raw.get("FRESHNESS_SLA_MINUTES", 30),
            "retrieval_adaptive_enabled": _as_bool(raw.get("RETRIEVAL_ADAPTIVE_ENABLED", "false")),
            "retrieval_backend": raw.get("RETRIEVAL_BACKEND", "existing"),
            "retrieval_database_url": raw.get("RETRIEVAL_DATABASE_URL", ""),
            "retrieval_content_enabled": _as_bool(raw.get("RETRIEVAL_CONTENT_ENABLED", "false")),
            "retrieval_content_key_file": raw.get("RETRIEVAL_CONTENT_KEY_FILE", ""),
            "retrieval_semantic_enabled": _as_bool(raw.get("RETRIEVAL_SEMANTIC_ENABLED", "false")),
            "retrieval_local_model_path": raw.get("RETRIEVAL_LOCAL_MODEL_PATH", ""),
            "database_url": raw.get("DATABASE_URL", "sqlite:///./data/morning_mail_pa.db"),
            "database_key_file": raw.get(
                "DATABASE_KEY_FILE",
                "./secrets/database.key" if raw.get("DATABASE_URL", "sqlite:///./data/morning_mail_pa.db")
                == "sqlite:///./data/morning_mail_pa.db" else "",
            ),
            "provider_credential_key_directory": raw.get(
                "PROVIDER_CREDENTIAL_KEY_DIRECTORY", "./secrets/provider-credential-keys"),
            "provider_credential_key_version": raw.get("PROVIDER_CREDENTIAL_KEY_VERSION", 1),
            "credential_key_directory": raw.get("CREDENTIAL_KEY_DIRECTORY", "./secrets/credential-keys"),
            "credential_active_key_version": raw.get("CREDENTIAL_ACTIVE_KEY_VERSION", "v1"),
            "microsoft_enabled": _as_bool(raw.get("MICROSOFT_ENABLED", "false")),
            "microsoft_client_id": raw.get("MICROSOFT_CLIENT_ID", ""),
            "microsoft_client_secret": raw.get("MICROSOFT_CLIENT_SECRET", ""),
            "microsoft_redirect_uri": raw.get("MICROSOFT_REDIRECT_URI", ""),
            "microsoft_allowed_emails": [e.strip().casefold() for e in raw.get("MICROSOFT_ALLOWED_EMAILS", "").split(",") if e.strip()],
            "email_provider": raw.get("EMAIL_PROVIDER", "demo"),
            "mailbox_id": raw.get("MAILBOX_ID", "default"),
            "google_client_secret_file": raw.get("GOOGLE_CLIENT_SECRET_FILE", "./secrets/google_client_secret.json"),
            "google_web_client_secret_file": raw.get("GOOGLE_WEB_CLIENT_SECRET_FILE", "./secrets/google_web_client_secret.json"),
            "hosted_oauth_redirect_uri": raw.get("HOSTED_OAUTH_REDIRECT_URI", ""),
            "hosted_allowed_emails": [email.strip().lower() for email in raw.get(
                "HOSTED_ALLOWED_EMAILS", "").split(",") if email.strip()],
            "google_token_file": raw.get("GOOGLE_TOKEN_FILE", "./secrets/google_token.json"),
            "google_send_token_file": raw.get("GOOGLE_SEND_TOKEN_FILE", "./secrets/google_send_token.json"),
            "google_calendar_token_file": raw.get("GOOGLE_CALENDAR_TOKEN_FILE", "./secrets/google_calendar_token.json"),
            "google_calendar_redirect_uri": raw.get("GOOGLE_CALENDAR_REDIRECT_URI", ""),
            "google_web_client_id": raw.get("GOOGLE_WEB_CLIENT_ID", ""),
            "google_web_client_secret": raw.get("GOOGLE_WEB_CLIENT_SECRET", ""),
            "google_web_redirect_uri": raw.get("GOOGLE_WEB_REDIRECT_URI", ""),
            "hosted_google_allowed_emails": [email.strip().casefold() for email in
                raw.get("HOSTED_GOOGLE_ALLOWED_EMAILS", "").split(",") if email.strip()],
            "gmail_pubsub_topic": raw.get("GMAIL_PUBSUB_TOPIC", ""),
            "gemini_live_model": raw.get("GEMINI_LIVE_MODEL", "gemini-3.8-live"),
            "gemini_live_fallback_models": [v.strip() for v in raw.get("GEMINI_LIVE_FALLBACK_MODELS", "gemini-3.1-flash-live-preview").split(",") if v.strip()],
            "voice_mode": raw.get("VOICE_MODE", "gemini_live"),
            "voice_ice_urls": [url.strip() for url in raw.get("VOICE_ICE_URLS", "").split(",") if url.strip()],
            "voice_ice_username": raw.get("VOICE_ICE_USERNAME", ""),
            "voice_ice_credential": raw.get("VOICE_ICE_CREDENTIAL", ""),
            "gemini_api_key": raw.get("GEMINI_API_KEY", ""),
            "gemini_reply_api_key": raw.get("GEMINI_REPLY_API_KEY", ""),
            "gemini_reply_model": raw.get("GEMINI_REPLY_MODEL", raw.get("GEMINI_MODEL", "gemini-3.5-flash-lite")),
            "gemini_model": raw.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            "entity_match_high_threshold": raw.get("ENTITY_MATCH_HIGH_THRESHOLD", 0.85),
            "entity_match_medium_threshold": raw.get("ENTITY_MATCH_MEDIUM_THRESHOLD", 0.60),
            "entity_match_min_margin": raw.get("ENTITY_MATCH_MIN_MARGIN", 0.08),
            "phone_api_token": raw.get("PHONE_API_TOKEN", ""),
            "phone_install_code": raw.get("PHONE_INSTALL_CODE", ""),
            "tts_enabled": _as_bool(str(raw.get("TTS_ENABLED", "true"))),
            "tts_provider": raw.get("TTS_PROVIDER", "system"),
            "tts_rate": raw.get("TTS_RATE", 165),
            "tts_volume": raw.get("TTS_VOLUME", 1.0),
            "log_level": raw.get("LOG_LEVEL", "INFO"),
            "scheduler_enabled": _as_bool(str(raw.get("SCHEDULER_ENABLED", "true"))),
            "enabled_categories": [
                value.strip().upper()
                for value in str(raw.get("EMAIL_CATEGORIES", ",".join(DEFAULT_CATEGORIES))).split(",")
                if value.strip()
            ],
        }
        return cls.model_validate(values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_environment()
