"""Atomic SQLite backups with integrity checks and bounded retention."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.database.db import database_key, encrypted_connection


def sqlite_path(settings: Settings) -> Path:
    prefix = "sqlite:///"
    if not settings.database_url.startswith(prefix) or settings.database_url == "sqlite:///:memory:":
        raise ValueError("Automated backup currently supports a file-backed SQLite database only.")
    path = Path(settings.database_url[len(prefix):])
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def backup_database(settings: Settings | None = None, keep: int = 14) -> Path:
    settings = settings or get_settings()
    source = sqlite_path(settings)
    if not source.is_file():
        raise FileNotFoundError("The database file does not exist.")
    destination_dir = PROJECT_ROOT / "data" / "backups"
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = destination_dir / f"morning-mail-{stamp}.db"
    temporary = destination.with_suffix(".tmp")
    key = database_key(settings.database_key_file)
    if settings.database_key_file and not key:
        raise RuntimeError("Database encryption key is missing; refusing a plaintext backup.")
    try:
        if key:
            with encrypted_connection(source, key) as src:
                src.execute(
                    f"ATTACH DATABASE ? AS backup KEY \"x'{key.hex()}'\"",
                    (str(temporary),),
                )
                src.execute("SELECT sqlcipher_export('backup')").fetchone()
                src.execute("DETACH DATABASE backup")
            with encrypted_connection(temporary, key) as dst:
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed.")
        else:
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(temporary) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed.")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    backups = sorted(destination_dir.glob("morning-mail-*.db"), reverse=True)
    for expired in backups[max(1, keep):]:
        expired.unlink()
    return destination


def latest_backup() -> Path | None:
    backup_dir = PROJECT_ROOT / "data" / "backups"
    return next(iter(sorted(backup_dir.glob("morning-mail-*.db"), reverse=True)), None)


def verify_backup(path: Path, settings: Settings | None = None) -> dict[str, object]:
    """Open a backup with the configured key and verify its essential schema."""
    settings = settings or get_settings()
    backup = Path(path)
    if not backup.is_file():
        raise FileNotFoundError("The backup file does not exist.")
    key = database_key(settings.database_key_file)
    if settings.database_key_file and not key:
        raise RuntimeError("Database encryption key is missing; refusing backup verification.")
    required = {"emails", "message_search", "sync_checkpoints"}
    if key:
        with encrypted_connection(backup, key) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()}
    else:
        with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()}
    if integrity != "ok":
        raise RuntimeError("Backup integrity check failed.")
    missing = required - tables
    if missing:
        raise RuntimeError(f"Backup schema is incomplete: {', '.join(sorted(missing))}")
    return {"path": str(backup), "integrity": integrity, "schema_valid": True}
