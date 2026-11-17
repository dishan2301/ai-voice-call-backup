#!/usr/bin/env python3
"""Rebuild derived FTS5 mail search data from canonical SQLite rows."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.database.db import create_tables, engine


def rebuild(connection) -> int:
    scoped = bool(connection.execute(text(
        "SELECT 1 FROM pragma_table_info('message_search') WHERE name = 'mailbox_id'"
    )).scalar())
    # Older releases stored bodies only in FTS. Preserve those bodies before
    # replacing the derived index, then require canonical text for every row.
    scope_predicate = "AND mailbox_id = emails.mailbox_id" if scoped else ""
    connection.execute(text(f"""
        UPDATE emails SET normalized_body = (
            SELECT body_text FROM message_search
            WHERE provider = emails.provider
              AND provider_message_id = emails.provider_message_id
              {scope_predicate}
            LIMIT 1
        )
        WHERE is_deleted = 0 AND normalized_body IS NULL
    """))
    missing = connection.execute(text("""
        SELECT count(*) FROM emails WHERE is_deleted = 0 AND normalized_body IS NULL
    """)).scalar_one()
    if missing:
        raise RuntimeError(f"Cannot rebuild search: {missing} active messages lack canonical body text")
    connection.execute(text("DELETE FROM message_search"))
    if scoped:
        insert_columns = "provider, mailbox_id, provider_message_id, sender_name, sender_email, subject, body_text, summary"
        select_columns = "e.provider, e.mailbox_id, e.provider_message_id, e.sender_name,"
    else:
        insert_columns = "provider, provider_message_id, sender_name, sender_email, subject, body_text, summary"
        select_columns = "e.provider, e.provider_message_id, e.sender_name,"
    connection.execute(text(f"""
        INSERT INTO message_search({insert_columns})
        SELECT {select_columns}
               e.sender_email, e.subject, e.normalized_body || COALESCE((
                   SELECT char(10) || group_concat(a.extracted_text, char(10))
                   FROM attachments a WHERE a.email_id = e.id AND a.extracted_text IS NOT NULL
               ), ''),
               COALESCE((
                   SELECT a.summary FROM email_analyses a
                   WHERE a.email_id = e.id
                   ORDER BY a.id DESC LIMIT 1
               ), '')
        FROM emails e WHERE e.is_deleted = 0
    """))
    return connection.execute(text("SELECT count(*) FROM message_search")).scalar_one()


def main() -> None:
    create_tables()
    with engine.begin() as connection:
        count = rebuild(connection)
    print(f"Rebuilt local mail search index with {count} messages.")


if __name__ == "__main__":
    main()
