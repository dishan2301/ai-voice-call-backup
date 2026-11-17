"""Explicit additive migrations; never called by application lifespan."""
import hashlib
from pathlib import Path
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2] / 'migrations' / 'retrieval'


def migrate(engine, *, vectors=False):
    if engine.dialect.name != 'postgresql':
        raise ValueError('Retrieval migrations require PostgreSQL')
    applied = []
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(81280321)'))
        conn.exec_driver_sql('CREATE TABLE IF NOT EXISTS m8r_schema_version (version text PRIMARY KEY, checksum text NOT NULL)')
        for filename in ['001_metadata.sql'] + (['002_vector.sql'] if vectors else []) + ['003_recovery.sql']:
            script = (ROOT / filename).read_text()
            digest = hashlib.sha256(script.encode()).hexdigest()
            old = conn.execute(text('SELECT checksum FROM m8r_schema_version WHERE version=:v'), {'v': filename}).scalar()
            if old:
                if old != digest:
                    raise ValueError('Applied retrieval migration checksum mismatch')
                continue
            conn.execute(text(script))
            conn.execute(text('INSERT INTO m8r_schema_version VALUES (:v,:h)'), {'v': filename, 'h': digest})
            applied.append(filename)
    return applied
