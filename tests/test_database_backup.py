import sqlite3
from pathlib import Path
import pytest

from app.core.config import Settings
from app.services import database_backup
from app.database.db import encrypted_connection
from scripts.migrate_database_encryption import encrypt_file


def test_backup_is_integral_private_and_retained(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence (value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('safe')")
    monkeypatch.setattr(database_backup, "PROJECT_ROOT", tmp_path)
    settings = Settings(database_url=f"sqlite:///{source}", database_key_file="")

    created = [database_backup.backup_database(settings, keep=2) for _ in range(3)]

    backups = sorted((tmp_path / "data/backups").glob("morning-mail-*.db"))
    assert len(backups) == 2
    assert created[-1] in backups
    assert backups[-1].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[-1]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM evidence").fetchone()[0] == "safe"


def test_migration_and_backup_keep_mailbox_content_encrypted(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    key_file = tmp_path / "database.key"
    key = bytes(range(32))
    key_file.write_bytes(key)
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence (value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('private mail body')")
    assert encrypt_file(source, key)
    assert not encrypt_file(source, key)
    assert source.read_bytes()[:16] != b"SQLite format 3\0"
    monkeypatch.setattr(database_backup, "PROJECT_ROOT", tmp_path)
    settings = Settings(database_url=f"sqlite:///{source}", database_key_file=str(key_file))

    backup = database_backup.backup_database(settings)

    assert backup.read_bytes()[:16] != b"SQLite format 3\0"
    assert b"private mail body" not in backup.read_bytes()
    with encrypted_connection(backup, key) as db:
        assert db.execute("SELECT value FROM evidence").fetchone()[0] == "private mail body"


def test_backup_refuses_plaintext_when_configured_key_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(database_backup, "PROJECT_ROOT", tmp_path)
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence (value TEXT)")
    settings = Settings(database_url=f"sqlite:///{source}",
                        database_key_file=str(tmp_path / "missing.key"))

    with pytest.raises(RuntimeError, match="refusing a plaintext backup"):
        database_backup.backup_database(settings)


def test_verify_encrypted_backup_checks_integrity_and_schema(tmp_path, monkeypatch):
    key_file = tmp_path / "database.key"
    key_file.write_bytes(b"k" * 32)
    source = tmp_path / "source.db"
    settings = Settings(database_url=f"sqlite:///{source}", database_key_file=str(key_file))
    with encrypted_connection(source, b"k" * 32) as db:
        db.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE sync_checkpoints (id INTEGER PRIMARY KEY)")
        db.execute("CREATE VIRTUAL TABLE message_search USING fts5(body_text)")
        db.commit()
    monkeypatch.setattr(database_backup, "PROJECT_ROOT", tmp_path)
    backup = database_backup.backup_database(settings)
    result = database_backup.verify_backup(backup, settings)
    assert result["integrity"] == "ok"
    assert result["schema_valid"] is True


@pytest.mark.parametrize('encrypted', [False, True])
def test_isolated_restore_preserves_data_schema_and_search(tmp_path, monkeypatch, encrypted):
    import shutil
    source = tmp_path / 'source.db'
    key_file = tmp_path / 'test.key'
    key_file.write_bytes(b'r' * 32)
    settings = Settings(database_url=f'sqlite:///{source}',
                        database_key_file=str(key_file) if encrypted else '')
    def connect(path):
        return encrypted_connection(path, b'r' * 32) if encrypted else sqlite3.connect(path)
    with connect(source) as db:
        db.execute('CREATE TABLE emails (id INTEGER PRIMARY KEY, body TEXT)')
        db.execute('CREATE TABLE sync_checkpoints (scope TEXT PRIMARY KEY)')
        db.execute('CREATE VIRTUAL TABLE message_search USING fts5(body_text)')
        db.execute("INSERT INTO emails VALUES (1, 'synthetic delivery evidence')")
        db.execute("INSERT INTO message_search VALUES ('synthetic delivery evidence')")
        db.commit()
    original = source.read_bytes()
    monkeypatch.setattr(database_backup, 'PROJECT_ROOT', tmp_path)
    backup = database_backup.backup_database(settings)
    restored = tmp_path / 'isolated-restored.db'
    shutil.copy2(backup, restored)
    assert database_backup.verify_backup(restored, settings)['schema_valid']
    with connect(restored) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT * FROM emails').fetchall() == [(1, 'synthetic delivery evidence')]
        assert db.execute("SELECT count(*) FROM message_search WHERE message_search MATCH 'delivery'").fetchone()[0] == 1
        db.execute("UPDATE emails SET body='isolated change'")
        db.commit()
    assert source.read_bytes() == original
    assert restored.stat().st_mode & 0o777 == 0o600
