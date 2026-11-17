from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import PROJECT_ROOT, get_settings


def database_key(path_value: str) -> bytes | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        return None
    key = path.read_bytes()
    if len(key) != 32:
        raise RuntimeError("Database encryption key must contain 32 bytes")
    return key


def encrypted_connection(database_path: Path, key: bytes):
    import sqlcipher3

    connection = sqlcipher3.connect(str(database_path), check_same_thread=False, timeout=30)
    connection.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return connection


class Base(DeclarativeBase):
    pass


def _sqlite_path(database_url: str):
    prefix = "sqlite:///"
    if database_url.startswith(prefix) and not database_url.startswith("sqlite:////"):
        return PROJECT_ROOT / database_url[len(prefix) :]
    if database_url.startswith("sqlite:////"):
        return Path(database_url[len("sqlite:///") :])
    return None


def _prepare_sqlite_path(database_url: str) -> None:
    path = _sqlite_path(database_url)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)


settings = get_settings()
_prepare_sqlite_path(settings.database_url)
connect_args = ({"check_same_thread": False, "timeout": 30}
                if settings.database_url.startswith("sqlite") else {})
_key = database_key(settings.database_key_file)
if settings.database_key_file and not _key and (
    settings.app_env == "production"
    or settings.database_url == "sqlite:///./data/morning_mail_pa.db"
):
    raise RuntimeError("Production database encryption key is missing")
if _key and _sqlite_path(settings.database_url):
    engine = create_engine(settings.database_url,
                           creator=lambda: encrypted_connection(_sqlite_path(settings.database_url), _key))
else:
    engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_tables() -> None:
    from app.database import models  # noqa: F401

    Base.metadata.create_all(engine)
    # This project does not use Alembic yet. Keep existing local databases
    # compatible with additive retrieval-pipeline fields.
    if settings.database_url.startswith("sqlite"):
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("emails")}
        if "is_deleted" not in columns:
            with engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE emails ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0"
                ))
        with engine.begin() as connection:
            for table, name, definition in (
                ("emails", "mailbox_id", "VARCHAR(120) NOT NULL DEFAULT 'default'"),
                ("emails", "recipients_json", "JSON NOT NULL DEFAULT '[]'"),
                ("emails", "cc_json", "JSON NOT NULL DEFAULT '[]'"),
                ("emails", "snippet", "TEXT NOT NULL DEFAULT ''"),
                ("emails", "labels_json", "JSON NOT NULL DEFAULT '[]'"),
                ("emails", "gmail_history_id", "VARCHAR(255)"),
                ("mailbox_people", "mailbox_id", "VARCHAR(120) NOT NULL DEFAULT 'default'"),
                ("conversation_state", "mailbox_id", "VARCHAR(120) NOT NULL DEFAULT 'default'"),
                ("conversation_state", "topics_json", "JSON NOT NULL DEFAULT '[]'"),
                ("conversation_state", "planner_context_json", "JSON NOT NULL DEFAULT '{}'"),
                ("agent_conversations", "mailbox_id", "VARCHAR(120) NOT NULL DEFAULT 'default'"),
                ("agent_memories", "mailbox_id", "VARCHAR(120) NOT NULL DEFAULT 'default'"),
                ("sync_checkpoints", "watch_history_id", "VARCHAR(255)"),
                ("sync_checkpoints", "watch_expiration_at", "DATETIME"),
                ("email_analyses", "analysis_version", "VARCHAR(100) NOT NULL DEFAULT 'v1'"),
                ("email_analyses", "source_hash", "VARCHAR(64)"),
                ("analysis_jobs", "analysis_version", "VARCHAR(100) NOT NULL DEFAULT 'v1'"),
                ("analysis_jobs", "watch_history_id", "VARCHAR(255)"),
                ("analysis_jobs", "watch_expiration_at", "DATETIME"),
            ):
                columns = {column["name"] for column in inspect(engine).get_columns(table)}
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
            attachment_columns = {column["name"] for column in inspect(engine).get_columns("attachments")}
            for name, definition in {
                "attachment_id": "VARCHAR(255)",
                "extraction_status": "VARCHAR(20) NOT NULL DEFAULT 'pending'",
                "extracted_text": "TEXT",
                "extraction_error_code": "VARCHAR(100)",
                "extraction_attempts": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "DATETIME",
                "leased_until": "DATETIME",
            }.items():
                if name not in attachment_columns:
                    connection.execute(text(f"ALTER TABLE attachments ADD COLUMN {name} {definition}"))
            schema = inspect(connection)
            fts_columns = ({column["name"] for column in schema.get_columns("message_search")}
                           if schema.has_table("message_search") else set())
            required_fts = {"mailbox_id", "keywords", "organization"}
            if fts_columns and not required_fts.issubset(fts_columns):
                connection.execute(text("ALTER TABLE message_search RENAME TO message_search_legacy"))
            connection.execute(text(
                """CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(
                    provider UNINDEXED, mailbox_id UNINDEXED, provider_message_id UNINDEXED,
                    sender_name, sender_email, subject, body_text, summary, keywords, organization,
                    tokenize='unicode61 remove_diacritics 2'
                )"""
            ))
            legacy = {row[0] for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'"))}
            if "message_search_legacy" in legacy:
                connection.execute(text("""INSERT INTO message_search(
                    provider, mailbox_id, provider_message_id, sender_name, sender_email,
                    subject, body_text, summary)
                    SELECT provider, 'default', provider_message_id, sender_name, sender_email,
                    subject, body_text, summary FROM message_search_legacy"""))
                connection.execute(text("DROP TABLE message_search_legacy"))
    path = _sqlite_path(settings.database_url)
    if path and path.exists():
        path.chmod(0o600)


async def get_db() -> AsyncGenerator[Session, None]:
    with SessionLocal() as session:
        yield session
