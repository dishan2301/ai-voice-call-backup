#!/usr/bin/env python3
"""Convert the stopped local mailbox and its backups to SQLCipher."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlcipher3

from app.core.config import PROJECT_ROOT, get_settings
from app.database.db import _sqlite_path, database_key, encrypted_connection


def encrypt_file(source: Path, key: bytes) -> bool:
    with source.open("rb") as handle:
        plaintext = handle.read(16) == b"SQLite format 3\0"
    if not plaintext:
        with encrypted_connection(source, key) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Encrypted database integrity check failed")
        return False
    temporary = source.with_name(source.name + ".encrypted.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with sqlcipher3.connect(str(source)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute(
                f"ATTACH DATABASE ? AS encrypted KEY \"x'{key.hex()}'\"",
                (str(temporary),),
            )
            connection.execute("SELECT sqlcipher_export('encrypted')").fetchone()
            connection.execute("DETACH DATABASE encrypted")
        os.chmod(temporary, 0o600)
        with encrypted_connection(temporary, key) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Encrypted database integrity check failed")
        os.replace(temporary, source)
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    settings = get_settings()
    path = _sqlite_path(settings.database_url)
    if path is None or not path.is_file():
        raise RuntimeError("Expected a file-backed SQLite mailbox database")
    key_path = Path(settings.database_key_file)
    if not key_path.is_absolute():
        key_path = PROJECT_ROOT / key_path
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path.parent.chmod(0o700)
    key = database_key(str(key_path))
    if key is None:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(os.urandom(32))
        key = database_key(str(key_path))
    files = [path, *sorted((PROJECT_ROOT / "data" / "backups").glob("morning-mail-*.db"))]
    converted = sum(encrypt_file(source, key) for source in files)
    print(f"Verified SQLCipher conversion of {converted} database files.")


if __name__ == "__main__":
    main()
