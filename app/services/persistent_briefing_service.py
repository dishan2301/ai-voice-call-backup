import logging
import threading
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor
from uuid import uuid4
from time import perf_counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, List, Optional, Protocol, Set

from googleapiclient.errors import HttpError

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.agents.briefing_agent import build_briefing
from app.agents.ranking import rank_analyses
from app.core.config import Settings, get_settings
from app.database.db import SessionLocal
from app.database.models import AnalysisJob, Attachment, Briefing, BriefingItem, BriefingRun, Commitment, Email, EmailAnalysis, MailboxPerson, SyncCheckpoint, SyncLease
from app.agents.commitments import extract_commitments
from app.email.base import EmailProvider
from app.email.thread_builder import build_threads
from app.schemas.analysis import Analysis
from app.schemas.briefing import BriefingDocument, RankedAnalysis
from app.schemas.mail import MailMessage


logger = logging.getLogger(__name__)
_SYNC_LOCK = threading.Lock()
_LEASE_DURATION = timedelta(minutes=5)
_LEASE_HEARTBEAT_SECONDS = 30
_MAX_ANALYSIS_ATTEMPTS = 3


def _extract_pdf_text_isolated(data: bytes, max_chars: int) -> str:
    from io import BytesIO
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)[:max_chars]


class Analyzer(Protocol):
    def analyze(self, card) -> Analysis:
        ...


class BriefingGenerationError(RuntimeError):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(f"Briefing generation failed during {stage}")


def _db_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_time(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _analysis_schema(row: EmailAnalysis) -> Analysis:
    return Analysis(
        importance=row.importance,
        category=row.category,
        needs_reply=row.needs_reply,
        needs_action=row.needs_action,
        urgency=row.urgency,
        sender=row.sender,
        subject=row.subject,
        summary=row.summary,
        why_important=row.why_important,
        requested_action=row.requested_action,
        deadline=row.deadline,
        key_facts=row.key_facts_json,
        risk_flags=row.risk_flags_json,
        confidence=row.confidence,
    )


class PersistentBriefingService:
    """Generate and atomically publish one briefing per local calendar day."""

    def __init__(
        self,
        provider: EmailProvider,
        analyzer: Analyzer,
        settings: Optional[Settings] = None,
        session_factory: sessionmaker = SessionLocal,
        mailbox_context=None,
    ):
        self.provider = provider
        self.analyzer = analyzer
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.mailbox_context = mailbox_context

    def _check_sync_authorization(self, session):
        if self.settings.deployment_mode == "hosted":
            from app.services.mailbox_credentials import MailboxCredentialError, MailboxCredentialStore
            context = self.mailbox_context
            if (context is None or context.provider != self.settings.email_provider
                    or context.mailbox_id != self.settings.mailbox_id):
                raise MailboxCredentialError("Authenticated sync context is required")
            MailboxCredentialStore._owned(session, context, self.settings.mailbox_id)

    @property
    def mailbox_scope(self) -> str:
        return f"{self.settings.email_provider}:{self.settings.mailbox_id}"

    @property
    def analysis_version(self) -> str:
        prompt = getattr(self.analyzer, "prompt_version", "unknown")
        return f"schema-v1:{prompt}"

    def generate(self, now: Optional[datetime] = None) -> BriefingDocument:
        if self.settings.deployment_mode == "hosted":
            raise BriefingGenerationError("HOSTED_BRIEFING_UNAVAILABLE")
        if not _SYNC_LOCK.acquire(blocking=False):
            raise BriefingGenerationError("SYNC_ALREADY_RUNNING")
        owner = uuid4().hex
        scope = self.mailbox_scope
        heartbeat_stop = threading.Event()
        heartbeat = None
        acquired = False
        try:
            if not self._acquire_lease(scope, owner):
                raise BriefingGenerationError("SYNC_ALREADY_RUNNING")
            acquired = True
            heartbeat = threading.Thread(
                target=self._heartbeat_lease, args=(scope, owner, heartbeat_stop), daemon=True
            )
            heartbeat.start()
            return self._generate_locked(now, owner)
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=5)
            try:
                if acquired:
                    self._release_lease(scope, owner)
            finally:
                _SYNC_LOCK.release()

    def sync_only(self, now: Optional[datetime] = None) -> int:
        """Synchronize Gmail into SQLite without waiting for Gemini analysis."""
        with self.session_factory() as session:
            self._check_sync_authorization(session)
        if not _SYNC_LOCK.acquire(blocking=False):
            raise BriefingGenerationError("SYNC_ALREADY_RUNNING")
        owner = uuid4().hex
        scope = self.mailbox_scope
        acquired = False
        try:
            if not self._acquire_lease(scope, owner):
                raise BriefingGenerationError("SYNC_ALREADY_RUNNING")
            acquired = True
            period_end = now or datetime.now(timezone.utc)
            with self.session_factory() as session:
                checkpoint = session.get(SyncCheckpoint, scope)
                period_start = (checkpoint.last_successful_sync_at.replace(tzinfo=timezone.utc)
                                if checkpoint and checkpoint.last_successful_sync_at
                                else period_end - timedelta(days=3650))
            messages, deleted_ids, checkpoint_id, full_sync = self._sync_messages(period_start, period_end)
            if full_sync and hasattr(self.provider, "iter_all_messages"):
                messages, deleted_ids, synced_count = self._commit_full_sync_pages(
                    messages, checkpoint_id, period_start, period_end, owner)
            else:
                self._commit_sync(messages, deleted_ids, checkpoint_id,
                                  full_sync=full_sync, lease_owner=owner)
                synced_count = len(messages)
            return synced_count
        finally:
            if acquired:
                self._release_lease(scope, owner)
            _SYNC_LOCK.release()

    def _acquire_lease(self, scope: str, owner: str) -> bool:
        now = datetime.utcnow()
        with self.session_factory() as session:
            result = session.execute(update(SyncLease).where(
                SyncLease.scope == scope, SyncLease.expires_at <= now,
            ).values(owner_id=owner, heartbeat_at=now, expires_at=now + _LEASE_DURATION))
            if result.rowcount:
                session.commit()
                return True
            try:
                with session.begin_nested():
                    session.add(SyncLease(scope=scope, owner_id=owner,
                                          heartbeat_at=now, expires_at=now + _LEASE_DURATION))
                    session.flush()
            except IntegrityError:
                session.rollback()
                return False
            session.commit()
            return True

    def _renew_lease(self, scope: str, owner: str) -> bool:
        now = datetime.utcnow()
        with self.session_factory() as session:
            result = session.execute(update(SyncLease).where(
                SyncLease.scope == scope, SyncLease.owner_id == owner,
                SyncLease.expires_at > now,
            ).values(heartbeat_at=now, expires_at=now + _LEASE_DURATION))
            session.commit()
            return result.rowcount == 1

    def _heartbeat_lease(self, scope: str, owner: str, stop: threading.Event) -> None:
        while not stop.wait(_LEASE_HEARTBEAT_SECONDS):
            try:
                if not self._renew_lease(scope, owner):
                    return
            except Exception as exc:
                logger.warning("sync_lease_heartbeat_failed", extra={"error_type": type(exc).__name__})

    def _release_lease(self, scope: str, owner: str) -> None:
        with self.session_factory() as session:
            session.execute(delete(SyncLease).where(
                SyncLease.scope == scope, SyncLease.owner_id == owner,
            ))
            session.commit()

    def _generate_locked(self, now: Optional[datetime] = None, lease_owner: Optional[str] = None) -> BriefingDocument:
        period_end = now or datetime.now(timezone.utc)
        if period_end.tzinfo is None:
            raise ValueError("Briefing generation time must be timezone-aware")
        period_key = period_end.astimezone(timezone.utc).isoformat(timespec="microseconds")

        with self.session_factory() as session:
            previous_end = session.scalar(
                select(BriefingRun.period_end)
                .where(
                    BriefingRun.status == "COMPLETE",
                    BriefingRun.provider == self.settings.email_provider,
                )
                .order_by(BriefingRun.period_end.desc())
                .limit(1)
            )
            period_start = (
                _utc_time(previous_end)
                if previous_end
                else period_end - timedelta(hours=self.settings.initial_lookback_hours)
            )
            run = BriefingRun(
                provider=self.settings.email_provider,
                period_key=period_key,
                period_start=_db_time(period_start),
                period_end=_db_time(period_end),
                status="GENERATING",
            )
            session.add(run)
            session.commit()

        stage = "EMAIL_SYNC"
        try:
            messages, deleted_ids, checkpoint_id, full_sync = self._sync_messages(period_start, period_end)
            if full_sync and hasattr(self.provider, "iter_all_messages"):
                messages, deleted_ids, _synced_count = self._commit_full_sync_pages(
                    messages, checkpoint_id, period_start, period_end, lease_owner
                )
            else:
                self._commit_sync(messages, deleted_ids, checkpoint_id,
                                  full_sync=full_sync, lease_owner=lease_owner)
            logger.info(
                "email_sync_complete",
                extra={"provider": self.settings.email_provider, "message_count": len(messages)},
            )
            cards = build_threads([mail for mail in messages
                                   if period_start <= mail.received_at < period_end])
            stage = "ANALYSIS"
            analysis_started = perf_counter()
            analyzed = []
            for card in cards:
                try:
                    result = self.analyzer.analyze(card)
                except Exception:
                    self.finish_analysis_for_message(card.newest.message_id, error_code="ANALYSIS_FAILED")
                    raise
                self.finish_analysis_for_message(card.newest.message_id)
                analyzed.append((card, result))
            logger.info(
                "email_analysis_complete",
                extra={
                    "provider": self.settings.email_provider,
                    "model": getattr(self.analyzer, "model", type(self.analyzer).__name__),
                    "analysis_count": len(analyzed),
                    "latency_ms": round((perf_counter() - analysis_started) * 1000, 2),
                },
            )
            stage = "PUBLISH"
            return self._publish(run.id, period_start, period_end, analyzed)
        except Exception as exc:
            with self.session_factory() as session:
                failed_run = session.get(BriefingRun, run.id)
                if failed_run:
                    failed_run.status = "INCOMPLETE"
                    failed_run.failure_stage = stage
                    failed_run.error_summary = type(exc).__name__
                    session.commit()
            logger.error(
                "briefing_generation_failed",
                extra={"run_id": run.id, "failure_stage": stage, "error_type": type(exc).__name__},
            )
            raise BriefingGenerationError(stage) from exc

    def _sync_messages(self, period_start, period_end):
        """Use incremental Gmail history after the first successful full sync."""
        if not all(hasattr(self.provider, name) for name in
                   ("fetch_incremental", "current_history_id")):
            return self.provider.fetch_messages(period_start, period_end), [], None, False
        scope = self.mailbox_scope
        with self.session_factory() as session:
            checkpoint = session.get(SyncCheckpoint, scope)
            start_history_id = checkpoint.last_history_id if checkpoint else None
        if start_history_id:
            try:
                messages, deleted_ids, next_history_id = self.provider.fetch_incremental(start_history_id)
                return messages, deleted_ids, (scope, next_history_id), False
            except HttpError as exc:
                if getattr(exc.resp, "status", None) != 404:
                    raise
                logger.warning("gmail_history_expired", extra={"provider": self.settings.email_provider})
        if not hasattr(self.provider, "fetch_all_messages"):
            raise BriefingGenerationError("FULL_SYNC_UNAVAILABLE")
        # Capture the cursor before listing. Changes during the scan are replayed
        # idempotently on the next history sync instead of falling through a gap.
        history_id = self.provider.current_history_id()
        if not history_id:
            raise BriefingGenerationError("MISSING_HISTORY_ID")
        fetch_all = getattr(self.provider, "iter_all_messages", self.provider.fetch_all_messages)
        return fetch_all(), [], (scope, history_id), True

    def _commit_full_sync_pages(self, pages, checkpoint_data, period_start, period_end, lease_owner):
        seen_ids = set()
        briefing_messages = []
        synced_count = 0
        for page in pages:
            if not isinstance(page, (list, tuple)):
                page = [page]
            if not page:
                continue
            synced_count += len(page)
            seen_ids.update(mail.message_id for mail in page)
            briefing_messages.extend(mail for mail in page
                                     if period_start <= mail.received_at < period_end)
            # Each page is independently searchable; the old checkpoint remains
            # unchanged until the complete scan and tombstone reconciliation finish.
            self._commit_sync(page, [], None, lease_owner=lease_owner)
        self._commit_sync([], [], checkpoint_data, full_sync=True,
                          lease_owner=lease_owner, full_sync_seen_ids=seen_ids)
        return briefing_messages, [], synced_count

    def _commit_sync(self, messages, deleted_ids, checkpoint_data, full_sync=False,
                     lease_owner=None, full_sync_seen_ids=None):
        """Publish canonical mail, search rows, and cursor in one transaction."""
        with self.session_factory() as session:
            if lease_owner is not None:
                scope = self.mailbox_scope
                now = datetime.utcnow()
                owned = session.execute(update(SyncLease).where(
                    SyncLease.scope == scope, SyncLease.owner_id == lease_owner,
                    SyncLease.expires_at > now,
                ).values(heartbeat_at=now, expires_at=now + _LEASE_DURATION))
                if owned.rowcount != 1:
                    raise BriefingGenerationError("SYNC_LEASE_LOST")
            self._check_sync_authorization(session)
            if full_sync:
                active_ids = (full_sync_seen_ids if full_sync_seen_ids is not None else
                              {mail.message_id for mail in messages})
                stored_ids = set(session.scalars(select(Email.provider_message_id).where(
                    Email.provider == self.settings.email_provider,
                    Email.mailbox_id == self.settings.mailbox_id,
                    Email.is_deleted.is_(False),
                )).all())
                deleted_ids = list(set(deleted_ids) | (stored_ids - set(active_ids)))
            self._persist_mail_evidence(messages, session)
            self._mark_deleted(deleted_ids, session)
            self._refresh_search_index(messages, session)
            self._commit_checkpoint(checkpoint_data, session, full_sync=full_sync)
            session.commit()

    def _mark_deleted(self, message_ids, session):
        if not message_ids:
            return
        rows = session.scalars(select(Email).where(
            Email.provider == self.settings.email_provider,
            Email.mailbox_id == self.settings.mailbox_id,
            Email.provider_message_id.in_(message_ids))).all()
        scoped_index = bool(session.execute(text(
            "SELECT 1 FROM pragma_table_info('message_search') WHERE name = 'mailbox_id'"
        )).scalar())
        if not scoped_index and self.settings.mailbox_id != "default":
            raise BriefingGenerationError("SEARCH_SCOPE_MIGRATION_REQUIRED")
        for row in rows:
            row.is_deleted = True
            sql = "DELETE FROM message_search WHERE provider = :provider AND provider_message_id = :id"
            if scoped_index:
                sql += " AND mailbox_id = :mailbox_id"
            session.execute(text(sql), {"provider": row.provider, "mailbox_id": row.mailbox_id,
                                       "id": row.provider_message_id})

    def _commit_checkpoint(self, checkpoint_data, session, full_sync=False):
        if not checkpoint_data or not checkpoint_data[1]:
            return
        scope, history_id = checkpoint_data
        if scope != self.mailbox_scope:
            raise BriefingGenerationError("CHECKPOINT_SCOPE_MISMATCH")
        checkpoint = session.get(SyncCheckpoint, scope)
        if checkpoint is None:
            checkpoint = SyncCheckpoint(scope=scope)
            session.add(checkpoint)
        checkpoint.last_history_id = history_id
        checkpoint.last_successful_sync_at = datetime.utcnow()
        if full_sync:
            checkpoint.last_full_sync_at = checkpoint.last_successful_sync_at
        checkpoint.status = "ready"
        checkpoint.error_code = None

    def _refresh_search_index(self, messages, session):
        """Keep the local lexical index aligned with canonical message rows."""
        if not messages:
            return
        connection = session.connection()
        for mail in messages:
            row = session.scalar(select(Email).where(
                Email.provider == self.settings.email_provider,
                Email.mailbox_id == self.settings.mailbox_id,
                Email.provider_message_id == mail.message_id))
            if not row or row.is_deleted:
                continue
            summary = session.scalar(select(EmailAnalysis.summary).where(
                EmailAnalysis.email_id == row.id).order_by(EmailAnalysis.id.desc()).limit(1)) or ""
            analysis_row = session.scalar(select(EmailAnalysis).where(
                EmailAnalysis.email_id == row.id).order_by(EmailAnalysis.id.desc()).limit(1))
            keywords = " ".join((analysis_row.requested_action if analysis_row else "",
                                  " ".join(analysis_row.key_facts_json if analysis_row else []),
                                  " ".join(analysis_row.risk_flags_json if analysis_row else [])))
            organization = (row.sender_email.split("@", 1)[-1].split(".")[0]
                            if "@" in (row.sender_email or "") else "")
            attachment_rows = session.execute(select(Attachment.filename, Attachment.extracted_text).where(
                Attachment.email_id == row.id)).all()
            attachment_text = "\n".join(" ".join(value for value in (filename, extracted) if value)
                                         for filename, extracted in attachment_rows)
            has_mailbox_scope = bool(connection.execute(text(
                "SELECT 1 FROM pragma_table_info('message_search') WHERE name = 'mailbox_id'"
            )).scalar())
            if has_mailbox_scope:
                connection.execute(text("DELETE FROM message_search WHERE provider = :provider AND mailbox_id = :mailbox_id AND provider_message_id = :id"),
                                   {"provider": row.provider, "mailbox_id": self.settings.mailbox_id, "id": row.provider_message_id})
                insert_sql = "INSERT INTO message_search(provider, mailbox_id, provider_message_id, sender_name, sender_email, subject, body_text, summary, keywords, organization) VALUES (:provider, :mailbox_id, :id, :sender_name, :sender_email, :subject, :body, :summary, :keywords, :organization)"
            else:
                if self.settings.mailbox_id != "default":
                    raise BriefingGenerationError("SEARCH_SCOPE_MIGRATION_REQUIRED")
                connection.execute(text("DELETE FROM message_search WHERE provider = :provider AND provider_message_id = :id"),
                                   {"provider": row.provider, "id": row.provider_message_id})
                insert_sql = "INSERT INTO message_search(provider, provider_message_id, sender_name, sender_email, subject, body_text, summary) VALUES (:provider, :id, :sender_name, :sender_email, :subject, :body, :summary)"
            params = {"provider": row.provider, "mailbox_id": self.settings.mailbox_id, "id": row.provider_message_id,
                                "sender_name": row.sender_name, "sender_email": row.sender_email,
                                "subject": row.subject, "body": (row.normalized_body or mail.body_text) +
                                (("\n" + attachment_text) if attachment_text else ""),
                                "summary": summary, "keywords": keywords, "organization": organization}
            connection.execute(text(insert_sql), params)

    def claim_analysis_jobs(self, limit: int = 20, lease_seconds: int = 300):
        """Claim pending or abandoned analysis work without touching Gmail state."""
        now = datetime.utcnow()
        with self.session_factory() as session:
            session.execute(update(AnalysisJob).where(
                AnalysisJob.email_id.in_(select(Email.id).where(
                    Email.provider == self.settings.email_provider,
                    Email.mailbox_id == self.settings.mailbox_id,
                )),
                AnalysisJob.status == "processing",
                AnalysisJob.leased_until <= now,
            ).values(status="pending", leased_until=None, error_code="LEASE_EXPIRED"))
            rows = session.scalars(select(AnalysisJob).join(Email).where(
                Email.provider == self.settings.email_provider,
                Email.mailbox_id == self.settings.mailbox_id, Email.is_deleted.is_(False),
                ((AnalysisJob.status == "pending") |
                 ((AnalysisJob.status == "failed") &
                  (AnalysisJob.attempts < _MAX_ANALYSIS_ATTEMPTS))),
            ).order_by(AnalysisJob.created_at).limit(max(1, min(limit, 100))).with_for_update()).all()
            for row in rows:
                row.status = "processing"
                row.attempts += 1
                row.leased_until = now + timedelta(seconds=lease_seconds)
            session.commit()
            return [row.id for row in rows]

    def finish_analysis_job(self, job_id: int, *, error_code: Optional[str] = None) -> None:
        with self.session_factory() as session:
            row = session.scalar(select(AnalysisJob).join(Email).where(
                AnalysisJob.id == job_id, Email.provider == self.settings.email_provider,
                Email.mailbox_id == self.settings.mailbox_id))
            if row is None:
                return
            row.status = "complete" if not error_code else (
                "failed" if row.attempts >= _MAX_ANALYSIS_ATTEMPTS else "pending"
            )
            row.error_code = error_code
            row.leased_until = None
            session.commit()

    def finish_analysis_for_message(self, message_id: str, *, error_code: Optional[str] = None) -> None:
        with self.session_factory() as session:
            row = session.scalar(select(AnalysisJob).join(Email).where(
                Email.provider == self.settings.email_provider,
                Email.mailbox_id == self.settings.mailbox_id,
                Email.provider_message_id == message_id,
            ))
            if row is not None:
                row.status = "complete" if not error_code else (
                    "failed" if row.attempts >= _MAX_ANALYSIS_ATTEMPTS else "pending"
                )
                row.error_code = error_code
                row.leased_until = None
                session.commit()

    def run_analysis_worker(self, limit: int = 20) -> dict:
        """Process a bounded batch of durable analysis jobs."""
        job_ids = self.claim_analysis_jobs(limit)
        completed = failed = 0
        for job_id in job_ids:
            try:
                with self.session_factory() as session:
                    job = session.get(AnalysisJob, job_id)
                    email = session.get(Email, job.email_id) if job else None
                    if email is None or email.is_deleted:
                        self.finish_analysis_job(job_id, error_code="MESSAGE_UNAVAILABLE")
                        failed += 1
                        continue
                    mail = MailMessage(
                        message_id=email.provider_message_id, thread_id=email.thread_id,
                        sender_name=email.sender_name, sender_email=email.sender_email,
                        subject=email.subject, received_at=_utc_time(email.received_at),
                        body_text=email.normalized_body or "",
                    )
                    cached = session.scalar(select(EmailAnalysis).where(
                        EmailAnalysis.email_id == email.id,
                        EmailAnalysis.analysis_version == self.analysis_version,
                        EmailAnalysis.source_hash == email.body_hash,
                    ).order_by(EmailAnalysis.id.desc()).limit(1))
                if cached is not None:
                    self.finish_analysis_job(job_id)
                    completed += 1
                    continue
                cards = build_threads([mail])
                if not cards:
                    raise ValueError("MESSAGE_NOT_ANALYZABLE")
                analysis = self.analyzer.analyze(cards[0])
                with self.session_factory() as session:
                    session.add(EmailAnalysis(
                        email_id=email.id, importance=analysis.importance.value,
                        category=analysis.category.value, needs_reply=analysis.needs_reply,
                        needs_action=analysis.needs_action, urgency=analysis.urgency.value,
                        sender=analysis.sender, subject=analysis.subject, summary=analysis.summary,
                        why_important=analysis.why_important, requested_action=analysis.requested_action,
                        deadline=analysis.deadline, key_facts_json=analysis.key_facts,
                        risk_flags_json=analysis.risk_flags, confidence=analysis.confidence,
                        model=getattr(self.analyzer, "model", type(self.analyzer).__name__),
                        prompt_version=getattr(self.analyzer, "prompt_version", "worker-v1"),
                        analysis_version=self.analysis_version,
                        source_hash=email.body_hash,
                    ))
                    session.commit()
                self.finish_analysis_job(job_id)
                completed += 1
            except Exception as exc:
                self.finish_analysis_job(job_id, error_code=type(exc).__name__[:100])
                failed += 1
        return {"claimed": len(job_ids), "completed": completed, "failed": failed}

    def run_attachment_worker(self, provider, limit: int = 10,
                              max_bytes: int = 10_000_000, max_chars: int = 200_000) -> dict:
        """Extract allowlisted attachment text without blocking Gmail sync."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.session_factory() as session:
            rows = session.scalars(select(Attachment).join(Email).where(
                Email.provider == self.settings.email_provider,
                Email.mailbox_id == self.settings.mailbox_id, Email.is_deleted.is_(False),
                Attachment.extraction_status.in_(["pending", "processing"]),
                Attachment.attachment_id.is_not(None),
                or_(Attachment.mime_type.in_(["application/pdf", "text/plain", "text/csv"]),
                    Attachment.mime_type.like("image/%")),
                or_(Attachment.next_attempt_at.is_(None), Attachment.next_attempt_at <= now),
                or_(Attachment.leased_until.is_(None), Attachment.leased_until <= now),
            ).order_by(Attachment.created_at).limit(max(1, min(limit, 50)))).all()
            ids = [row.id for row in rows]
            for row in rows:
                row.extraction_status = "processing"
                row.leased_until = now + timedelta(minutes=10)
            session.commit()
        completed = failed = 0
        for attachment_id in ids:
            try:
                with self.session_factory() as session:
                    row = session.get(Attachment, attachment_id)
                    email = session.get(Email, row.email_id) if row else None
                if row is None or email is None:
                    raise ValueError("ATTACHMENT_MESSAGE_MISSING")
                data = provider.download_attachment(email.provider_message_id, row.attachment_id,
                                                     max_size=max_bytes)
                if row.mime_type.startswith("image/"):
                    result = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6"],
                                            input=data, capture_output=True, timeout=30, check=False)
                    if result.returncode:
                        raise RuntimeError("OCR_FAILED")
                    text_value = result.stdout.decode("utf-8", errors="replace")
                elif row.mime_type == "application/pdf":
                    with ProcessPoolExecutor(max_workers=1) as pool:
                        text_value = pool.submit(_extract_pdf_text_isolated, data, max_chars).result(timeout=30)
                else:
                    text_value = data.decode("utf-8", errors="replace")
                with self.session_factory() as session:
                    stored = session.get(Attachment, attachment_id)
                    stored.extracted_text = text_value[:max_chars]
                    stored.extraction_status = "complete"
                    stored.extraction_error_code = None
                    stored.leased_until = None
                    session.commit()
                completed += 1
            except Exception as exc:
                with self.session_factory() as session:
                    stored = session.get(Attachment, attachment_id)
                    if stored:
                        stored.extraction_attempts += 1
                        stored.extraction_status = "failed" if stored.extraction_attempts >= 3 else "pending"
                        stored.extraction_error_code = type(exc).__name__[:100]
                        stored.next_attempt_at = (datetime.now(timezone.utc).replace(tzinfo=None) +
                                                  timedelta(minutes=2 ** min(stored.extraction_attempts, 5)))
                        stored.leased_until = None
                        session.commit()
                failed += 1
        return {"claimed": len(ids), "completed": completed, "failed": failed}

    def register_gmail_watch(self, provider, topic_name: str, label_ids=None) -> dict:
        """Register push delivery and persist expiration; notifications trigger history sync only."""
        result = provider.watch_mailbox(topic_name, label_ids=label_ids)
        expiration = result.get("expiration")
        expiration_at = None
        if expiration:
            expiration_at = datetime.fromtimestamp(int(expiration) / 1000, tz=timezone.utc).replace(tzinfo=None)
        with self.session_factory() as session:
            checkpoint = session.get(SyncCheckpoint, self.mailbox_scope)
            if checkpoint is None:
                checkpoint = SyncCheckpoint(scope=self.mailbox_scope)
                session.add(checkpoint)
            checkpoint.watch_history_id = str(result.get("historyId", "")) or None
            checkpoint.watch_expiration_at = expiration_at
            session.commit()
        return {"history_id": result.get("historyId"), "expiration_at": expiration_at}

    def renew_gmail_watch_if_needed(self, provider, topic_name: str, label_ids=None) -> dict:
        """Renew a watch before expiry; return a deterministic no-op when still fresh."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        scope = self.mailbox_scope
        with self.session_factory() as session:
            checkpoint = session.get(SyncCheckpoint, scope)
            expiration = checkpoint.watch_expiration_at if checkpoint else None
        if expiration and expiration > now + timedelta(hours=24):
            return {"renewed": False, "expiration_at": expiration}
        result = self.register_gmail_watch(provider, topic_name, label_ids=label_ids)
        return {"renewed": True, **result}


    def _publish(self, run_id: int, period_start: datetime, period_end: datetime, analyzed):
        current_thread_ids = {card.thread_id for card, _ in analyzed}
        ranked = rank_analyses((analysis, card.newest.received_at) for card, analysis in analyzed)
        with self.session_factory() as session:
            outstanding = self._outstanding(
                session, current_thread_ids, period_end, self.settings.email_provider
            )
            start_rank = len(ranked) + 1
            ranked.extend(
                RankedAnalysis(rank=start_rank + index, section="Still Needs Attention", analysis=analysis)
                for index, analysis in enumerate(outstanding)
            )
            document = build_briefing(
                ranked,
                briefing_date=period_end.astimezone(self.settings.timezone).date(),
                period_start=period_start,
                period_end=period_end,
            )

            analysis_ids = {}
            for card, analysis in analyzed:
                mail = card.newest
                email_row = session.scalar(
                    select(Email).where(
                        Email.provider == self.settings.email_provider,
                        Email.mailbox_id == self.settings.mailbox_id,
                        Email.provider_message_id == mail.message_id,
                    )
                )
                if not email_row:
                    email_row = Email(
                        provider=self.settings.email_provider,
                        mailbox_id=self.settings.mailbox_id,
                        provider_message_id=mail.message_id,
                        thread_id=mail.thread_id,
                        sender_name=mail.sender_name,
                        sender_email=mail.sender_email,
                        recipients_json=mail.recipient_emails,
                        cc_json=mail.cc_emails,
                        subject=mail.subject,
                        snippet=mail.snippet,
                        labels_json=mail.labels,
                        gmail_history_id=mail.gmail_history_id or None,
                        received_at=_db_time(mail.received_at),
                        body_hash=sha256(mail.body_text.encode("utf-8")).hexdigest(),
                        normalized_body=mail.body_text,
                    )
                    session.add(email_row)
                    session.flush()
                else:
                    # Gmail is authoritative. Re-syncs must refresh mutable
                    # headers/body instead of leaving the first observed copy
                    # as stale local truth.
                    email_row.thread_id = mail.thread_id
                    email_row.sender_name = mail.sender_name
                    email_row.sender_email = mail.sender_email
                    email_row.recipients_json = mail.recipient_emails
                    email_row.cc_json = mail.cc_emails
                    email_row.subject = mail.subject
                    email_row.snippet = mail.snippet
                    email_row.labels_json = mail.labels
                    email_row.gmail_history_id = mail.gmail_history_id or email_row.gmail_history_id
                    email_row.received_at = _db_time(mail.received_at)
                    email_row.body_hash = sha256(mail.body_text.encode("utf-8")).hexdigest()
                    email_row.is_deleted = False
                    email_row.normalized_body = mail.body_text
                row = EmailAnalysis(
                    email_id=email_row.id,
                    importance=analysis.importance.value,
                    category=analysis.category.value,
                    needs_reply=analysis.needs_reply,
                    needs_action=analysis.needs_action,
                    urgency=analysis.urgency.value,
                    sender=analysis.sender,
                    subject=analysis.subject,
                    summary=analysis.summary,
                    why_important=analysis.why_important,
                    requested_action=analysis.requested_action,
                    deadline=analysis.deadline,
                    key_facts_json=analysis.key_facts,
                    risk_flags_json=analysis.risk_flags,
                    confidence=analysis.confidence,
                    model=getattr(self.analyzer, "model", type(self.analyzer).__name__),
                    prompt_version=getattr(self.analyzer, "prompt_version", "demo-v1"),
                )
                session.add(row)
                session.flush()
                analysis_ids[id(analysis)] = row.id

            briefing = Briefing(
                briefing_date=document.briefing_date,
                generated_at=_db_time(period_end),
                period_start=_db_time(period_start),
                period_end=_db_time(period_end),
                total_emails=document.total_emails,
                critical_count=document.critical_count,
                high_count=document.high_count,
                reply_count=document.reply_count,
                action_count=document.action_count,
                low_priority_count=document.low_priority_count,
                detailed_brief=document.detailed_brief,
                spoken_brief=document.spoken_brief,
                status="READY",
            )
            session.add(briefing)
            session.flush()

            for item in document.items:
                analysis_id = analysis_ids.get(id(item.analysis))
                if analysis_id is None:
                    analysis_id = session.scalar(
                        select(EmailAnalysis.id)
                        .where(
                            EmailAnalysis.sender == item.analysis.sender,
                            EmailAnalysis.subject == item.analysis.subject,
                        )
                        .order_by(EmailAnalysis.created_at.desc())
                        .limit(1)
                    )
                session.add(
                    BriefingItem(
                        briefing_id=briefing.id,
                        email_analysis_id=analysis_id,
                        rank=item.rank,
                        section=item.section,
                        user_status=(
                            "NEEDS_REPLY"
                            if item.analysis.needs_reply
                            else "NEEDS_ACTION"
                            if item.analysis.needs_action
                            else "NO_ACTION"
                        ),
                    )
                )
            run = session.get(BriefingRun, run_id)
            run.status = "COMPLETE"
            run.briefing_id = briefing.id
            session.commit()
            logger.info(
                "briefing_generation_complete",
                extra={
                    "run_id": run_id,
                    "briefing_id": briefing.id,
                    "thread_count": document.total_emails,
                    "critical_count": document.critical_count,
                    "high_count": document.high_count,
                },
            )
            return document

    def _persist_mail_evidence(self, messages, session):
        """Keep literal promises useful even when model analysis is unavailable."""
        for mail in messages:
            email = session.scalar(select(Email).where(
                Email.provider == self.settings.email_provider,
                Email.mailbox_id == self.settings.mailbox_id,
                Email.provider_message_id == mail.message_id))
            if not email:
                email = Email(provider=self.settings.email_provider, mailbox_id=self.settings.mailbox_id,
                              provider_message_id=mail.message_id, thread_id=mail.thread_id,
                              sender_name=mail.sender_name, sender_email=mail.sender_email,
                              recipients_json=mail.recipient_emails, cc_json=mail.cc_emails,
                              subject=mail.subject, snippet=mail.snippet, labels_json=mail.labels,
                              gmail_history_id=mail.gmail_history_id or None,
                              received_at=_db_time(mail.received_at),
                              body_hash=sha256(mail.body_text.encode("utf-8")).hexdigest(),
                              normalized_body=mail.body_text)
                session.add(email)
                session.flush()
            else:
                email.thread_id = mail.thread_id
                email.sender_name = mail.sender_name
                email.sender_email = mail.sender_email
                email.recipients_json = mail.recipient_emails
                email.cc_json = mail.cc_emails
                email.subject = mail.subject
                email.snippet = mail.snippet
                email.labels_json = mail.labels
                email.gmail_history_id = mail.gmail_history_id or email.gmail_history_id
                email.received_at = _db_time(mail.received_at)
                email.body_hash = sha256(mail.body_text.encode("utf-8")).hexdigest()
                email.is_deleted = False
                email.normalized_body = mail.body_text
            job = session.scalar(select(AnalysisJob).where(AnalysisJob.email_id == email.id))
            if job is None:
                session.add(AnalysisJob(email_id=email.id, source_hash=email.body_hash,
                                        analysis_version=self.analysis_version))
            elif job.source_hash != email.body_hash or job.analysis_version != self.analysis_version:
                job.source_hash = email.body_hash
                job.analysis_version = self.analysis_version
                job.status = "pending"
                job.attempts = 0
                job.error_code = None
            current_attachments = {(item.filename, item.mime_type, item.size) for item in mail.attachments}
            stored_attachments = session.scalars(select(Attachment).where(Attachment.email_id == email.id)).all()
            for attachment in stored_attachments:
                if (attachment.filename, attachment.mime_type, attachment.size_bytes) not in current_attachments:
                    session.delete(attachment)
            for item in mail.attachments:
                existing = session.scalar(select(Attachment).where(
                    Attachment.email_id == email.id,
                    Attachment.filename == item.filename,
                    Attachment.mime_type == item.mime_type,
                ))
                if existing is None:
                    image_unsupported = item.mime_type.lower().startswith("image/") and shutil.which("tesseract") is None
                    session.add(Attachment(email_id=email.id, filename=item.filename,
                                           mime_type=item.mime_type, size_bytes=item.size,
                                           attachment_id=item.attachment_id or None,
                                           extraction_status=("unsupported" if image_unsupported else ("complete" if item.mime_type in {"text/plain", "text/csv"}
                                                              and not item.attachment_id
                                                              and item.size <= 200_000 else "pending")),
                                           extraction_error_code=("OCR_UNAVAILABLE" if image_unsupported else None)))
                else:
                    existing.size_bytes = item.size
                    existing.attachment_id = item.attachment_id or existing.attachment_id
            if mail.sender_email:
                person = session.scalar(select(MailboxPerson).where(
                    MailboxPerson.provider == self.settings.email_provider,
                    MailboxPerson.mailbox_id == self.settings.mailbox_id,
                    MailboxPerson.email == mail.sender_email.lower(),
                ))
                if person is None:
                    session.add(MailboxPerson(
                        provider=self.settings.email_provider,
                        mailbox_id=self.settings.mailbox_id,
                        email=mail.sender_email.lower(), canonical_name=mail.sender_name,
                        message_count=1, first_seen_at=_db_time(mail.received_at),
                        last_seen_at=_db_time(mail.received_at),
                    ))
                else:
                    person.message_count += 1
                    person.last_seen_at = max(person.last_seen_at or _db_time(mail.received_at),
                                              _db_time(mail.received_at))
                    if mail.sender_name and not person.canonical_name:
                        person.canonical_name = mail.sender_name
                    elif mail.sender_name and mail.sender_name != person.canonical_name:
                        aliases = list(person.aliases_json or [])
                        if mail.sender_name not in aliases:
                            aliases.append(mail.sender_name)
                            person.aliases_json = aliases[-20:]
            for item in extract_commitments(mail.body_text, mail.labels):
                if not session.scalar(select(Commitment.id).where(
                        Commitment.email_id == email.id,
                        Commitment.source_text == item["text"])):
                    session.add(Commitment(email_id=email.id, direction=item["direction"],
                                           source_text=item["text"], due_text=item["due_text"]))

    @staticmethod
    def _outstanding(
        session: Session,
        current_thread_ids: Set[str],
        now: datetime,
        provider: str,
    ) -> List[Analysis]:
        rows = session.execute(
            select(EmailAnalysis, Email, BriefingItem)
            .join(Email, Email.id == EmailAnalysis.email_id)
            .join(BriefingItem, BriefingItem.email_analysis_id == EmailAnalysis.id)
            .where(
                Email.provider == provider,
                EmailAnalysis.importance.in_(["CRITICAL", "HIGH"]),
                BriefingItem.user_status.in_(["NEW", "NEEDS_REPLY", "NEEDS_ACTION", "WAITING"]),
            )
            .order_by(EmailAnalysis.created_at.desc())
        ).all()
        results: List[Analysis] = []
        seen_threads: Set[str] = set()
        for analysis_row, email_row, item_row in rows:
            if email_row.thread_id in current_thread_ids or email_row.thread_id in seen_threads:
                continue
            if item_row.snoozed_until and _utc_time(item_row.snoozed_until) > now:
                continue
            seen_threads.add(email_row.thread_id)
            results.append(_analysis_schema(analysis_row))
            if len(results) == 10:
                break
        return results

    @staticmethod
    def _load_document(session: Session, briefing_id: int) -> BriefingDocument:
        briefing = session.get(Briefing, briefing_id)
        item_rows = session.execute(
            select(BriefingItem, EmailAnalysis)
            .join(EmailAnalysis, EmailAnalysis.id == BriefingItem.email_analysis_id)
            .where(BriefingItem.briefing_id == briefing_id)
            .order_by(BriefingItem.rank)
        ).all()
        items = [
            RankedAnalysis(rank=item.rank, section=item.section, analysis=_analysis_schema(analysis))
            for item, analysis in item_rows
        ]
        return BriefingDocument(
            briefing_date=briefing.briefing_date,
            period_start=_utc_time(briefing.period_start),
            period_end=_utc_time(briefing.period_end),
            total_emails=briefing.total_emails,
            critical_count=briefing.critical_count,
            high_count=briefing.high_count,
            reply_count=briefing.reply_count,
            action_count=briefing.action_count,
            low_priority_count=briefing.low_priority_count,
            detailed_brief=briefing.detailed_brief,
            spoken_brief=briefing.spoken_brief,
            items=items,
        )

    def latest_cached_document(self) -> BriefingDocument | None:
        """Return the newest completed cached briefing without running sync or analysis."""
        with self.session_factory() as session:
            row = session.scalar(select(Briefing).join(
                BriefingRun, BriefingRun.briefing_id == Briefing.id
            ).where(
                BriefingRun.provider == self.settings.email_provider,
                BriefingRun.status == "COMPLETE",
            ).order_by(Briefing.generated_at.desc()).limit(1))
            return self._load_document(session, row.id) if row else None
