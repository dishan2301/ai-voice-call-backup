from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine, func, select, text
from googleapiclient.errors import HttpError
from httplib2 import Response
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.email_analyzer import DemoAnalyzer
from app.core.config import Settings
from app.database.db import Base
from app.database.models import AnalysisJob, Attachment, Briefing, BriefingRun, Commitment, Email, EmailAnalysis, MailboxPerson, SyncCheckpoint, SyncLease
from app.email.base import EmailProvider
from app.schemas.mail import MailMessage
from app.services.persistent_briefing_service import BriefingGenerationError, PersistentBriefingService, _SYNC_LOCK
from scripts.rebuild_mail_search import rebuild


class StaticProvider(EmailProvider):
    def __init__(self, messages):
        self.messages = messages
        self.windows = []

    def fetch_messages(self, since, until):
        self.windows.append((since, until))
        return [mail for mail in self.messages if since <= mail.received_at < until]


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("""CREATE VIRTUAL TABLE message_search USING fts5(
            provider UNINDEXED, provider_message_id UNINDEXED,
            sender_name, sender_email, subject, body_text, summary
        )"""))
    return sessionmaker(bind=engine, expire_on_commit=False)


def _important_mail(received_at):
    return MailMessage(
        message_id="important-1",
        thread_id="important-thread",
        sender_name="Customer",
        sender_email="customer@example.com",
        subject="Service down",
        received_at=received_at,
        body_text="The service is unavailable. Please investigate immediately and update us today.",
    )


def test_full_sync_deletion_keeps_other_mailbox_with_same_message_id() -> None:
    sessions = _session_factory()
    with sessions() as session:
        session.execute(text("DROP TABLE message_search"))
        session.execute(text("""CREATE VIRTUAL TABLE message_search USING fts5(
            provider UNINDEXED, mailbox_id UNINDEXED, provider_message_id UNINDEXED,
            sender_name, sender_email, subject, body_text, summary, keywords, organization
        )"""))
        session.commit()
    services = {
        box: PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                       Settings(email_provider="gmail", mailbox_id=box), sessions)
        for box in ("a", "b")
    }
    mail = _important_mail(datetime(2026, 9, 14, 9, tzinfo=timezone.utc))
    for box, service in services.items():
        service._commit_sync([mail], [], (f"gmail:{box}", "100"))
    services["a"]._commit_sync([], [], ("gmail:a", "101"), full_sync=True,
                               full_sync_seen_ids=set())
    with sessions() as session:
        rows = session.execute(select(Email.mailbox_id, Email.is_deleted).order_by(Email.mailbox_id)).all()
        indexed = session.execute(text("SELECT mailbox_id FROM message_search")).scalars().all()
    assert rows == [("a", True), ("b", False)]
    assert indexed == ["b"]


def test_overlapping_sync_is_rejected_without_fetching_mail() -> None:
    provider = StaticProvider([])
    service = PersistentBriefingService(provider, DemoAnalyzer(), Settings(email_provider="demo"), _session_factory())
    _SYNC_LOCK.acquire()
    try:
        with pytest.raises(BriefingGenerationError, match="SYNC_ALREADY_RUNNING"):
            service.generate(datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc))
    finally:
        _SYNC_LOCK.release()
    assert provider.windows == []


def test_search_index_failure_does_not_advance_mail_or_checkpoint() -> None:
    sessions = _session_factory()
    service = PersistentBriefingService(
        StaticProvider([]), DemoAnalyzer(), Settings(email_provider="gmail"), sessions
    )
    with sessions() as session:
        session.execute(text("DROP TABLE message_search"))
        session.commit()
    mail = _important_mail(datetime(2026, 9, 14, 9, tzinfo=timezone.utc))

    with pytest.raises(Exception):
        service._commit_sync([mail], [], ("gmail:default", "123"))

    with sessions() as session:
        assert session.scalar(select(func.count(Email.id))) == 0
        assert session.get(SyncCheckpoint, "gmail:default") is None


def test_expiring_database_lease_rejects_overlap_and_stale_owner_commit() -> None:
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    scope = "gmail:default"
    assert service._acquire_lease(scope, "first")
    assert not service._acquire_lease(scope, "second")
    with sessions() as session:
        lease = session.get(SyncLease, scope)
        lease.expires_at = datetime.utcnow() - timedelta(seconds=1)
        session.commit()
    assert service._acquire_lease(scope, "second")
    with pytest.raises(BriefingGenerationError, match="SYNC_LEASE_LOST"):
        service._commit_sync([_important_mail(datetime.now(timezone.utc))], [],
                             (scope, "999"), lease_owner="first")
    service._release_lease(scope, "first")
    with sessions() as session:
        assert session.get(SyncLease, scope).owner_id == "second"
        assert session.get(SyncCheckpoint, scope) is None
        assert session.scalar(select(func.count(Email.id))) == 0
    service._release_lease(scope, "second")


def test_fts_rebuild_recovers_legacy_body_and_excludes_deleted_mail() -> None:
    sessions = _session_factory()
    with sessions() as session:
        session.add_all([
            Email(provider="gmail", provider_message_id="active", thread_id="a",
                  sender_name="Buyer", sender_email="buyer@example.test", subject="Order",
                  received_at=datetime.utcnow(), body_hash="a"),
            Email(provider="gmail", provider_message_id="deleted", thread_id="b",
                  sender_name="Buyer", sender_email="buyer@example.test", subject="Old",
                  received_at=datetime.utcnow(), body_hash="b", is_deleted=True),
        ])
        session.execute(text("""INSERT INTO message_search
            (provider, provider_message_id, subject, body_text)
            VALUES ('gmail', 'active', 'Order', 'The attendance device stopped.'),
                   ('gmail', 'deleted', 'Old', 'Removed content')"""))
        session.commit()
    with sessions.kw["bind"].begin() as connection:
        assert rebuild(connection) == 1
    with sessions() as session:
        assert session.scalar(select(Email.normalized_body).where(Email.provider_message_id == "active")) == "The attendance device stopped."
        assert session.execute(text("SELECT count(*) FROM message_search WHERE message_search MATCH 'attendance'")).scalar_one() == 1
        assert session.execute(text("SELECT count(*) FROM message_search WHERE message_search MATCH 'Removed'")).scalar_one() == 0


def test_fts_rebuild_preserves_existing_index_when_body_is_unrecoverable() -> None:
    sessions = _session_factory()
    with sessions() as session:
        session.add(Email(provider="gmail", provider_message_id="missing", thread_id="a",
                          sender_name="Buyer", sender_email="buyer@example.test", subject="Order",
                          received_at=datetime.utcnow(), body_hash="a"))
        session.execute(text("""INSERT INTO message_search
            (provider, provider_message_id, subject) VALUES ('gmail', 'orphan', 'Retained')"""))
        session.commit()
    with pytest.raises(RuntimeError, match="lack canonical body"):
        with sessions.kw["bind"].begin() as connection:
            rebuild(connection)
    with sessions() as session:
        assert session.execute(text("SELECT count(*) FROM message_search WHERE message_search MATCH 'Retained'")).scalar_one() == 1


def test_initial_full_sync_indexes_older_mail_without_putting_it_in_today_brief() -> None:
    class FullProvider(StaticProvider):
        def current_history_id(self):
            return "100"

        def fetch_all_messages(self):
            return self.messages

        def fetch_incremental(self, start_history_id):
            assert start_history_id == "100"
            return [], [], "101"

    now = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
    old = _important_mail(now - timedelta(days=10))
    sessions = _session_factory()
    service = PersistentBriefingService(
        FullProvider([old]), DemoAnalyzer(), Settings(email_provider="gmail"), sessions
    )

    document = service.generate(now)

    assert document.total_emails == 0
    with sessions() as session:
        assert session.scalar(select(func.count(Email.id))) == 1
        assert session.get(SyncCheckpoint, "gmail:default").last_history_id == "100"
        assert session.execute(text("SELECT count(*) FROM message_search WHERE message_search MATCH 'Service' ")).scalar_one() == 1
    service.generate(now + timedelta(minutes=1))


def test_expired_history_reconciles_removed_messages() -> None:
    class ExpiredProvider(StaticProvider):
        def current_history_id(self):
            return "200"

        def fetch_all_messages(self):
            return self.messages

        def fetch_incremental(self, start_history_id):
            raise HttpError(Response({"status": 404}), b"history expired")

    now = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
    provider = ExpiredProvider([_important_mail(now - timedelta(hours=1))])
    sessions = _session_factory()
    service = PersistentBriefingService(provider, DemoAnalyzer(), Settings(email_provider="gmail"), sessions)
    service.generate(now)
    provider.messages = []

    service.generate(now + timedelta(minutes=1))

    with sessions() as session:
        assert session.scalar(select(Email.is_deleted)) is True
        assert session.get(SyncCheckpoint, "gmail:default").last_history_id == "200"
        assert session.execute(text("SELECT count(*) FROM message_search")).scalar_one() == 0


def test_full_sync_page_failure_keeps_old_checkpoint_and_commits_searchable_pages():
    now = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    with sessions() as session:
        session.add(SyncCheckpoint(scope="gmail:default", last_history_id="old"))
        session.commit()
    assert service._acquire_lease("gmail:default", "owner")

    def interrupted_pages():
        yield [_important_mail(now - timedelta(hours=1))]
        raise RuntimeError("page fetch failed")

    with pytest.raises(RuntimeError, match="page fetch failed"):
        service._commit_full_sync_pages(interrupted_pages(), ("gmail:default", "new"),
                                        now - timedelta(days=1), now, "owner")

    with sessions() as session:
        assert session.get(SyncCheckpoint, "gmail:default").last_history_id == "old"
        assert session.scalar(select(func.count(Email.id))) == 1
        assert session.execute(text("SELECT count(*) FROM message_search WHERE message_search MATCH 'service'")).scalar_one() == 1


def test_analysis_jobs_are_durable_and_expired_claims_are_requeued():
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    mail = _important_mail(datetime.now(timezone.utc))
    service._commit_sync([mail], [], None)
    claimed = service.claim_analysis_jobs()
    assert len(claimed) == 1
    with sessions() as session:
        job = session.get(AnalysisJob, claimed[0])
        job.leased_until = datetime.utcnow() - timedelta(seconds=1)
        session.commit()
    assert service.claim_analysis_jobs() == [claimed[0]]
    service.finish_analysis_job(claimed[0])
    with sessions() as session:
        job = session.get(AnalysisJob, claimed[0])
        assert job.status == "complete"
        assert job.leased_until is None


def test_analysis_worker_consumes_a_claimed_message_and_writes_analysis():
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    mail = _important_mail(datetime.now(timezone.utc))
    service._commit_sync([mail], [], None)

    assert service.run_analysis_worker() == {"claimed": 1, "completed": 1, "failed": 0}
    with sessions() as session:
        assert session.scalar(select(func.count(EmailAnalysis.id))) == 1
        assert session.scalar(select(AnalysisJob.status)) == "complete"


def test_analysis_worker_retries_failures_then_dead_letters_after_three_attempts():
    class BrokenAnalyzer:
        def analyze(self, card):
            raise RuntimeError("temporary model failure")

    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), BrokenAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    service._commit_sync([_important_mail(datetime.now(timezone.utc))], [], None)
    for attempt in range(3):
        result = service.run_analysis_worker()
        assert result == {"claimed": 1, "completed": 0, "failed": 1}
    assert service.run_analysis_worker() == {"claimed": 0, "completed": 0, "failed": 0}
    with sessions() as session:
        job = session.scalar(select(AnalysisJob))
        assert job.status == "failed"
        assert job.attempts == 3


def test_sync_persists_attachment_metadata_and_marks_unsafe_extraction_pending():
    from app.schemas.mail import AttachmentMetadata
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    mail = _important_mail(datetime.now(timezone.utc)).model_copy(update={
        "attachments": [
            AttachmentMetadata(filename="notes.txt", mime_type="text/plain", size=100),
            AttachmentMetadata(filename="invoice.pdf", mime_type="application/pdf", size=5000),
        ], "has_attachments": True,
    })
    service._commit_sync([mail], [], None)
    with sessions() as session:
        rows = session.scalars(select(Attachment).order_by(Attachment.filename)).all()
        assert [(row.filename, row.extraction_status) for row in rows] == [
            ("invoice.pdf", "pending"), ("notes.txt", "complete")
        ]


def test_attachment_worker_extracts_bounded_text_and_records_status():
    class AttachmentProvider:
        def download_attachment(self, message_id, attachment_id, max_size):
            assert (message_id, attachment_id) == ("important-1", "att-1")
            return b"employee_id,status\n42,missing"

    from app.schemas.mail import AttachmentMetadata
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    mail = _important_mail(datetime.now(timezone.utc)).model_copy(update={
        "attachments": [AttachmentMetadata(filename="attendance.csv", mime_type="text/csv",
                                            size=28, attachment_id="att-1")],
    })
    service._commit_sync([mail], [], None)
    assert service.run_attachment_worker(AttachmentProvider()) == {
        "claimed": 1, "completed": 1, "failed": 0
    }
    with sessions() as session:
        row = session.scalar(select(Attachment))
        assert row.extraction_status == "complete"
        assert "42,missing" in row.extracted_text


def test_image_attachment_is_explicitly_unsupported_without_ocr():
    from app.schemas.mail import AttachmentMetadata
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    mail = _important_mail(datetime.now(timezone.utc)).model_copy(update={
        "attachments": [AttachmentMetadata(filename="scan.png", mime_type="image/png",
                                            size=4096, attachment_id="img-1")],
    })
    service._commit_sync([mail], [], None)
    with sessions() as session:
        row = session.scalar(select(Attachment))
        assert row.extraction_status == "unsupported"
        assert row.extraction_error_code == "OCR_UNAVAILABLE"


def test_attachment_worker_retries_transient_failures_before_dead_letter():
    class FlakyProvider:
        def download_attachment(self, message_id, attachment_id, max_size):
            raise TimeoutError("temporary")

    from app.schemas.mail import AttachmentMetadata
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    mail = _important_mail(datetime.now(timezone.utc)).model_copy(update={
        "attachments": [AttachmentMetadata(filename="attendance.csv", mime_type="text/csv",
                                            size=28, attachment_id="att-flaky")],
    })
    service._commit_sync([mail], [], None)
    for attempt in range(3):
        result = service.run_attachment_worker(FlakyProvider())
        assert result["failed"] == 1
        with sessions() as session:
            row = session.scalar(select(Attachment))
            assert row.extraction_attempts == attempt + 1
            if attempt < 2:
                assert row.extraction_status == "pending"
                row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
                session.commit()
            else:
                assert row.extraction_status == "failed"


def test_sync_builds_mailbox_observed_sender_vocabulary():
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)
    mail = _important_mail(datetime.now(timezone.utc))
    service._commit_sync([mail, mail.model_copy(update={"message_id": "important-2"})], [], None)
    with sessions() as session:
        person = session.scalar(select(MailboxPerson))
        assert person.email == "customer@example.com"
        assert person.canonical_name == "Customer"
        assert person.message_count == 2


def test_each_same_day_run_is_saved_and_fetches_only_after_previous_success() -> None:
    now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    provider = StaticProvider([_important_mail(now - timedelta(hours=2))])
    sessions = _session_factory()
    service = PersistentBriefingService(
        provider,
        DemoAnalyzer(),
        Settings(email_provider="demo", initial_lookback_hours=24),
        sessions,
    )

    first = service.generate(now)
    second = service.generate(now + timedelta(minutes=5))

    assert first.total_emails == 1
    assert second.total_emails == 0
    assert "no new emails since your previous briefing" in second.spoken_brief.lower()
    assert "no new emails since your previous briefing" in second.detailed_brief.lower()
    assert "no new important emails" in second.spoken_brief.lower()
    assert provider.windows == [
        (now - timedelta(hours=24), now),
        (now, now + timedelta(minutes=5)),
    ]
    with sessions() as session:
        assert session.scalar(select(func.count(Briefing.id))) == 2
        assert session.scalar(select(func.count(BriefingRun.id))) == 2


def test_next_day_starts_after_success_and_keeps_unresolved_high_item() -> None:
    first_time = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    provider = StaticProvider([_important_mail(first_time - timedelta(hours=2))])
    sessions = _session_factory()
    service = PersistentBriefingService(
        provider,
        DemoAnalyzer(),
        Settings(email_provider="demo"),
        sessions,
    )
    service.generate(first_time)
    provider.messages = []

    second = service.generate(first_time + timedelta(days=1))

    assert provider.windows[-1][0] == first_time
    assert second.total_emails == 0
    assert any(item.section == "Still Needs Attention" for item in second.items)
    assert "Still Needs Attention" in second.detailed_brief


def test_sync_failure_is_recorded_without_a_fake_brief() -> None:
    class FailingProvider(EmailProvider):
        def fetch_messages(self, since, until):
            raise RuntimeError("private provider error body")

    sessions = _session_factory()
    service = PersistentBriefingService(
        FailingProvider(), DemoAnalyzer(), Settings(email_provider="demo"), sessions
    )

    with pytest.raises(BriefingGenerationError, match="EMAIL_SYNC"):
        service.generate(datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc))

    with sessions() as session:
        run = session.scalar(select(BriefingRun))
        assert run.status == "INCOMPLETE"
        assert run.failure_stage == "EMAIL_SYNC"
        assert run.error_summary == "RuntimeError"
        assert session.scalar(select(func.count(Briefing.id))) == 0


@pytest.mark.parametrize("failure", [TimeoutError("gmail timeout"),
                                      HttpError(Response({"status": 401}), b"unauthorized"),
                                      HttpError(Response({"status": 429}), b"quota")])
def test_gmail_timeout_auth_and_quota_failures_are_separate_sync_failures(failure) -> None:
    class FailingProvider(EmailProvider):
        def fetch_messages(self, since, until):
            raise failure

    sessions = _session_factory()
    service = PersistentBriefingService(
        FailingProvider(), DemoAnalyzer(), Settings(email_provider="gmail"), sessions
    )
    with pytest.raises(BriefingGenerationError, match="EMAIL_SYNC"):
        service.generate(datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc))
    with sessions() as session:
        run = session.scalar(select(BriefingRun))
        assert run.failure_stage == "EMAIL_SYNC"
        assert run.error_summary in {"TimeoutError", "HttpError"}


def test_provider_windows_and_outstanding_items_do_not_mix() -> None:
    first_time = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    sessions = _session_factory()
    PersistentBriefingService(
        StaticProvider([_important_mail(first_time - timedelta(hours=2))]),
        DemoAnalyzer(),
        Settings(email_provider="demo"),
        sessions,
    ).generate(first_time)
    gmail_provider = StaticProvider([])

    result = PersistentBriefingService(
        gmail_provider,
        DemoAnalyzer(),
        Settings(email_provider="gmail", initial_lookback_hours=24),
        sessions,
    ).generate(first_time + timedelta(days=1))

    assert gmail_provider.windows == [
        (first_time, first_time + timedelta(days=1))
    ]
    assert result.items == []


def test_literal_commitment_survives_model_analysis_failure() -> None:
    class FailingAnalyzer:
        def analyze(self, card):
            raise RuntimeError("model unavailable")

    now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    mail = MailMessage(message_id="promise-1", thread_id="promise-thread",
                       sender_name="Customer", sender_email="customer@example.com",
                       subject="Report", received_at=now - timedelta(hours=1),
                       body_text="We will send the final report by Friday.", labels=["INBOX"])
    sessions = _session_factory()
    service = PersistentBriefingService(StaticProvider([mail]), FailingAnalyzer(),
                                        Settings(email_provider="gmail"), sessions)

    with pytest.raises(BriefingGenerationError, match="ANALYSIS"):
        service.generate(now)

    with sessions() as session:
        row = session.scalar(select(Commitment))
        assert row.direction == "THEY_OWE"
        assert row.source_text == "We will send the final report by Friday."
        assert row.due_text == "by Friday"
