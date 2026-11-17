from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.db import Base


def utc_now() -> datetime:
    return datetime.utcnow()


class ReceptionistSettings(Base):
    """Owner-scoped, disabled-by-default AI receptionist configuration."""
    __tablename__ = "receptionist_settings"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id", "mailbox_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailbox_connections.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allowlist_json: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class User(Base):
    """An application principal; provider identities are deliberately separate."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="MEMBER")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class MailboxConnection(Base):
    """Provider account metadata and encrypted credential material for one owner."""
    __tablename__ = "mailbox_connections"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    provider_account_id: Mapped[str] = mapped_column(String(320))
    verified_sender: Mapped[str] = mapped_column(String(320))
    credential_ciphertext: Mapped[str] = mapped_column(Text, default="")
    credential_key_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AuthSession(Base):
    """Opaque, revocable server session. Only its SHA-256 digest is persisted."""
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailbox_connections.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class OAuthTransaction(Base):
    """Single-use, short-lived server-side state for an OAuth authorization attempt."""
    __tablename__ = "oauth_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    browser_binding_hash: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    code_verifier_ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (UniqueConstraint("mailbox_id", "capability"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailbox_connections.id"), index=True)
    capability: Mapped[str] = mapped_column(String(20))
    ciphertext: Mapped[str] = mapped_column(Text)
    key_version: Mapped[str] = mapped_column(String(40))
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class Email(Base):
    __tablename__ = "emails"
    __table_args__ = (UniqueConstraint("provider", "mailbox_id", "provider_message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), default="default", index=True)
    provider_message_id: Mapped[str] = mapped_column(String(255))
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    sender_name: Mapped[str] = mapped_column(String(255), default="")
    sender_email: Mapped[str] = mapped_column(String(320), default="")
    recipients_json: Mapped[list] = mapped_column(JSON, default=list)
    cc_json: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(998), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    labels_json: Mapped[list] = mapped_column(JSON, default=list)
    gmail_history_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    body_hash: Mapped[str] = mapped_column(String(64), index=True)
    normalized_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    analyses: Mapped[List["EmailAnalysis"]] = relationship(back_populates="email")


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("email_id", "filename", "mime_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(150), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    attachment_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    extracted_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extraction_error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extraction_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    leased_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class MailboxPerson(Base):
    __tablename__ = "mailbox_people"
    __table_args__ = (UniqueConstraint("provider", "mailbox_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), default="default", index=True)
    email: Mapped[str] = mapped_column(String(320))
    canonical_name: Mapped[str] = mapped_column(String(255), default="")
    aliases_json: Mapped[list] = mapped_column(JSON, default=list)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="mailbox_observed")


class EmailAnalysis(Base):
    __tablename__ = "email_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    importance: Mapped[str] = mapped_column(String(20), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    needs_reply: Mapped[bool] = mapped_column(Boolean)
    needs_action: Mapped[bool] = mapped_column(Boolean)
    urgency: Mapped[str] = mapped_column(String(30))
    sender: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(998))
    summary: Mapped[str] = mapped_column(Text)
    why_important: Mapped[str] = mapped_column(Text, default="")
    requested_action: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    key_facts_json: Mapped[list] = mapped_column(JSON, default=list)
    risk_flags_json: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(50))
    analysis_version: Mapped[str] = mapped_column(String(100), default="v1")
    source_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    email: Mapped[Email] = relationship(back_populates="analyses")


class AnalysisJob(Base):
    """Durable analysis work item; message storage does not depend on it."""
    __tablename__ = "analysis_jobs"
    __table_args__ = (UniqueConstraint("email_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_version: Mapped[str] = mapped_column(String(100), default="v1")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    leased_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    watch_history_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    watch_expiration_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class BriefingRun(Base):
    __tablename__ = "briefing_runs"
    __table_args__ = (UniqueConstraint("provider", "period_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    period_key: Mapped[str] = mapped_column(String(255))
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), index=True)
    failure_stage: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    briefing_id: Mapped[Optional[int]] = mapped_column(ForeignKey("briefings.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class Briefing(Base):
    __tablename__ = "briefings"

    id: Mapped[int] = mapped_column(primary_key=True)
    briefing_date: Mapped[date] = mapped_column(Date, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    total_emails: Mapped[int] = mapped_column(Integer, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    action_count: Mapped[int] = mapped_column(Integer, default=0)
    low_priority_count: Mapped[int] = mapped_column(Integer, default=0)
    detailed_brief: Mapped[str] = mapped_column(Text)
    spoken_brief: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="READY")
    prompt_version: Mapped[str] = mapped_column(String(50), default="v1")
    consent_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)

    items: Mapped[List["BriefingItem"]] = relationship(back_populates="briefing")


class BriefingItem(Base):
    __tablename__ = "briefing_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    briefing_id: Mapped[int] = mapped_column(ForeignKey("briefings.id"), index=True)
    email_analysis_id: Mapped[int] = mapped_column(ForeignKey("email_analyses.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(50))
    user_status: Mapped[str] = mapped_column(String(30), default="NEW")
    snoozed_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    briefing: Mapped[Briefing] = relationship(back_populates="items")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class DailyBriefingSchedule(Base):
    __tablename__ = "daily_briefing_schedules"
    __table_args__ = (UniqueConstraint("user_id", "mailbox_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), index=True)
    workspace_id: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(30))
    legacy_local: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    time: Mapped[str] = mapped_column(String(5), default="09:00")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Kolkata")
    vip_senders: Mapped[list] = mapped_column(JSON, default=list)
    catchup_minutes: Mapped[int] = mapped_column(default=180)


class DailyBriefingRun(Base):
    __tablename__ = "daily_briefing_runs"
    __table_args__ = (UniqueConstraint("user_id", "mailbox_id", "local_date"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), index=True)
    local_date: Mapped[str] = mapped_column(String(10))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    attempts: Mapped[int] = mapped_column(default=0)
    lease: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    failure_code: Mapped[str] = mapped_column(String(50), default="")
    document: Mapped[dict] = mapped_column(JSON, default=dict)


class SyncCheckpoint(Base):
    """Durable mailbox sync state; one row per provider/account scope."""
    __tablename__ = "sync_checkpoints"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_history_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_successful_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_full_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="never", index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class SyncLease(Base):
    """Exclusive, expiring ownership of a mailbox sync."""
    __tablename__ = "sync_leases"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentConversation(Base):
    __tablename__ = "agent_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), default="default", index=True)
    session_key: Mapped[str] = mapped_column(String(255), index=True)
    user_message: Mapped[str] = mapped_column(Text)
    assistant_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class ConversationState(Base):
    __tablename__ = "conversation_state"
    __table_args__ = (UniqueConstraint("mailbox_id", "session_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), default="default", index=True)
    session_key: Mapped[str] = mapped_column(String(255), index=True)
    result_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    topics_json: Mapped[list] = mapped_column(JSON, default=list)
    planner_context_json: Mapped[dict] = mapped_column(JSON, default=dict)
    selected_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    selected_thread_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class PlannerAction(Base):
    __tablename__ = "planner_actions"
    __table_args__ = (UniqueConstraint("user_id", "mailbox_id", "session_key", "fingerprint"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), index=True)
    session_key: Mapped[str] = mapped_column(String(255), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="PREPARED")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(80))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(30), default="SCHEDULED", index=True)
    gmail_thread_id: Mapped[Optional[str]] = mapped_column(String(255))
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    delivery_token_hash: Mapped[str] = mapped_column(String(64), default="")
    notification_status: Mapped[str] = mapped_column(String(30), default="PENDING")


class PlannerAudit(Base):
    __tablename__ = "planner_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), index=True)
    resource_id: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(120), default="default", index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str] = mapped_column(String(255), index=True)
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(255))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent_conversations.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(40), index=True)
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), index=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AgentAudit(Base):
    __tablename__ = "agent_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent_conversations.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(String(40))
    tool_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class DraftReview(Base):
    __tablename__ = "draft_reviews"

    draft_id: Mapped[int] = mapped_column(ForeignKey("agent_actions.id"), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64))
    ticket_hash: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Commitment(Base):
    __tablename__ = "commitments"
    __table_args__ = (UniqueConstraint("email_id", "source_text"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    direction: Mapped[str] = mapped_column(String(20), index=True)
    source_text: Mapped[str] = mapped_column(Text)
    due_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
