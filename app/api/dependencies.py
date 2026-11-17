import json
from datetime import datetime

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import MailboxContext, require_mailbox_context
from app.core.config import get_settings
from app.database.db import SessionLocal
from app.agents.email_analyzer import DemoAnalyzer
from app.agents.llm_analyzer import LLMEmailAnalyzer
from app.agents.llm_analyzer import LLMAnalysisError
from app.email.demo import DemoEmailProvider
from app.email.gmail import GmailProvider
from app.agents.mail_agent import MailAgent
from app.llm.gemini import GeminiProvider
from app.services.persistent_briefing_service import PersistentBriefingService
from app.services.settings_service import get_runtime_settings
from app.services.mailbox_credentials import MailboxCredentialError
from app.core.provider_credentials import load_credential, store_credential


class HonestFallbackAnalyzer:
    """Keep mail usable during provider outages and label the degraded result."""
    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.model = primary.provider.model
        self.prompt_version = getattr(primary, "prompt_version", "v1")
        self.provider_down = False

    def analyze(self, card):
        try:
            if not self.provider_down:
                return self.primary.analyze(card)
        except LLMAnalysisError:
            self.provider_down = True
        result = self.fallback.analyze(card)
        return result.model_copy(update={
            "risk_flags": list(dict.fromkeys([*result.risk_flags, "DEGRADED_RULES_ONLY"])),
            "confidence": min(result.confidence, 0.4),
        })


def create_generation_service(context: MailboxContext | None = None,
                              session_factory=SessionLocal) -> PersistentBriefingService:
    settings = get_settings()
    if settings.deployment_mode == "hosted":
        if context is None or context.legacy_local or context.provider != "gmail":
            raise ValueError("An authenticated Gmail mailbox context is required")
        settings = settings.model_copy(update={
            "email_provider": context.provider, "mailbox_id": context.mailbox_id,
            "vip_senders": [], "vip_domains": [], "important_keywords": [],
        })
        provider = GmailProvider.from_mailbox(settings, context, session_factory)
    else:
        settings = get_runtime_settings()
        provider = None
    if settings.email_provider == "demo":
        return PersistentBriefingService(
            DemoEmailProvider(),
            DemoAnalyzer(
                settings.vip_senders,
                settings.vip_domains,
                settings.important_keywords,
                settings.enabled_categories,
            ),
            settings,
            session_factory,
            mailbox_context=context,
        )
    if settings.email_provider == "gmail":
        rules = {
            "vip_senders": settings.vip_senders,
            "vip_domains": settings.vip_domains,
            "important_keywords": settings.important_keywords,
            "enabled_categories": settings.enabled_categories,
        }
        return PersistentBriefingService(
            provider if provider is not None else GmailProvider.from_settings(settings),
            HonestFallbackAnalyzer(LLMEmailAnalyzer(
                GeminiProvider(settings.gemini_api_key, settings.gemini_model),
                importance_rules=rules,
            ), DemoAnalyzer(**rules)),
            settings,
            session_factory,
            mailbox_context=context,
        )
    raise ValueError(f"Unsupported EMAIL_PROVIDER: {settings.email_provider}")


def _credential_persister(context: MailboxContext, capability: str, scopes: list[str], settings):
    def persist(serialized: str) -> None:
        info = json.loads(serialized)
        expiry = info.get("expiry")
        expires_at = datetime.fromisoformat(expiry).replace(tzinfo=None) if expiry else None
        granted_scopes = info.get("scopes") or scopes
        if isinstance(granted_scopes, str):
            granted_scopes = granted_scopes.split()
        with SessionLocal() as db:
            store_credential(db, context, capability, info, granted_scopes, expires_at, settings)
    return persist


def _hosted_gmail_provider(context: MailboxContext, settings, db: Session | None = None):
    if context.provider != "gmail":
        raise HTTPException(status_code=503, detail="This hosted provider is not available yet")
    def load_grants(session):
        read_data, read_scopes = load_credential(session, context, "READ", settings)
        try:
            send_data, send_scopes = load_credential(session, context, "SEND", settings)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            send_data, send_scopes = None, None
        return read_data, read_scopes, send_data, send_scopes
    if db is None:
        with SessionLocal() as session:
            read_info, read_scopes, send_info, send_scopes = load_grants(session)
    else:
        read_info, read_scopes, send_info, send_scopes = load_grants(db)
        db.rollback()
    return GmailProvider.from_authorized_user_info(
        read_info, read_scopes, settings, send_info=send_info, send_scopes=send_scopes,
        persist_read_credential=_credential_persister(context, "READ", read_scopes, settings),
        persist_send_credential=(
            _credential_persister(context, "SEND", send_scopes, settings) if send_info else None
        ),
    )


def create_outlook_provider(context):
    from app.email.outlook import OutlookProvider
    from app.services.mailbox_credentials import MailboxCredentialStore
    from app.core.auth import validate_mailbox_context
    from app.database.models import MailboxConnection
    settings=get_settings()
    if not settings.microsoft_enabled or context.provider!="microsoft" or context.legacy_local:
        raise HTTPException(403,"An authenticated enabled Outlook connection is required")
    store=MailboxCredentialStore(settings.provider_credential_key_directory,settings.provider_credential_key_version)
    with SessionLocal() as db:
        store.get(db,context,context.mailbox_id)
        connection=db.get(MailboxConnection,context.mailbox_id)
        subject,email=connection.provider_account_id,connection.verified_sender
    def authorize():
        with SessionLocal() as db:
            validate_mailbox_context(db,context)
    def load():
        with SessionLocal() as db:
            bundle=store.get(db,context,context.mailbox_id)
            cipher=db.get(MailboxConnection,context.mailbox_id).credential_ciphertext
            return bundle['read'],cipher
    def save(grant,expected):
        with SessionLocal() as db:
            store.put(db,context,context.mailbox_id,{'read':grant},expected_ciphertext=expected)
    return OutlookProvider(settings,load,save,authorize,subject,email)


def create_mail_agent(context: MailboxContext, credential_db: Session | None = None) -> MailAgent:
    settings = get_runtime_settings()
    if not context.legacy_local:
        settings = settings.model_copy(update={
            "email_provider": context.provider, "mailbox_id": context.mailbox_id,
            "google_token_file": "", "google_send_token_file": "",
        })
        provider = create_outlook_provider(context) if context.provider == "microsoft" else _hosted_gmail_provider(context, settings, credential_db)
    else:
        provider = None
    return MailAgent(settings=settings, session_factory=SessionLocal,
                     gmail=provider, mailbox_context=context)


def create_assistant(context):
    if context.provider == "microsoft":
        return create_mail_agent(context)
    class Assistant:
        def run(self, message, session_key, **options):
            from app.api.planner import should_handle, assistant_request
            if should_handle(message, session_key, context):
                try:
                    return assistant_request(message, session_key, context, options.get("cancelled", lambda: False))
                except Exception:
                    return {"state": "ERROR", "answer": "Calendar or reminders could not complete this request. No confirmed change was made.",
                            "spoken_answer": "I could not complete that Calendar or reminder request. Please try again."}
            return create_mail_agent(context).run(message, session_key, **options)
    return Assistant()


async def get_generation_service(context: MailboxContext = Depends(require_mailbox_context)) -> PersistentBriefingService:
    try:
        return create_generation_service(context)
    except MailboxCredentialError as exc:
        raise HTTPException(403, "Mailbox connection is no longer available") from exc
    except ValueError as exc:
        raise HTTPException(503, "Mail refresh is not configured for this connection") from exc
