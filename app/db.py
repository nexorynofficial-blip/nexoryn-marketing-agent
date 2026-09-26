"""SQLite connection, schema management, and small row<->dataclass helpers.

Single-tenant, local-first app: one file, no external database service. The
helpers below exist so slack_app.py (and later pipeline.py/dedupe.py) share
one place that knows how to read/write rows, instead of scattering raw SQL
across the app.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from app.models import Classification, Draft, Language, Message, MessageStatus, MessageType, Platform

# Built from the enum (not hand-copied) so the DB CHECK constraint and
# MessageStatus can never drift apart.
_STATUS_VALUES_SQL = ", ".join(f"'{status.value}'" for status in MessageStatus)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'facebook')),
    type TEXT NOT NULL CHECK (type IN ('comment', 'dm')),
    sender_id TEXT NOT NULL,
    sender_username TEXT,
    text TEXT NOT NULL,
    detected_language TEXT CHECK (detected_language IN ('en', 'ur_script', 'ur_roman', 'other')),
    english_translation TEXT,
    received_at TEXT NOT NULL,
    classification TEXT CHECK (classification IN ('lead', 'question', 'compliment', 'complaint', 'spam', 'other')),
    status TEXT NOT NULL DEFAULT 'pending_draft' CHECK (status IN ({_STATUS_VALUES_SQL}))
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL REFERENCES messages(id),
    draft_text TEXT NOT NULL,
    draft_language TEXT,
    is_handoff INTEGER NOT NULL DEFAULT 0,
    slack_message_ts TEXT,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    sent_at TEXT,
    -- The human's replacement text from the Edit modal, kept separate from
    -- draft_text so both the AI's original and what was actually approved
    -- stay recoverable from the DB alone.
    edited_text TEXT
);

CREATE TABLE IF NOT EXISTS processed_webhook_ids (
    webhook_event_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_message_id ON drafts(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Additive-only migrations applied after CREATE TABLE IF NOT EXISTS, for
# columns added after a dev DB already exists on disk. (table, column, DDL type)
_MIGRATIONS = [
    ("drafts", "edited_text", "TEXT"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl_type in _MIGRATIONS:
        existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    conn.commit()


def _messages_table_matches_current_status_check(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
    ).fetchone()
    return row is not None and f"'{MessageStatus.ALERTED.value}'" in row["sql"]


def _migrate_messages_status_check(conn: sqlite3.Connection) -> None:
    """SQLite can't ALTER a CHECK constraint in place, so when a dev DB
    predates a new MessageStatus value, recreate the table (same columns,
    updated CHECK) and copy every row across.

    legacy_alter_table avoids SQLite silently rewriting drafts.message_id's
    REFERENCES clause to point at messages_old when we rename messages out
    of the way -- without it, the later DROP TABLE messages_old fails (or
    worse, leaves drafts pointing at a table that no longer exists).
    """
    if _messages_table_matches_current_status_check(conn):
        return

    messages_create_sql = SCHEMA.split("CREATE TABLE IF NOT EXISTS drafts")[0]

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.executescript(
            f"""
            ALTER TABLE messages RENAME TO messages_old;
            {messages_create_sql}
            INSERT INTO messages SELECT * FROM messages_old;
            DROP TABLE messages_old;
            """
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _apply_migrations(conn)
        _migrate_messages_status_check(conn)
    finally:
        conn.close()


# ---- row <-> dataclass mapping ----


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        platform=Platform(row["platform"]),
        type=MessageType(row["type"]),
        sender_id=row["sender_id"],
        sender_username=row["sender_username"],
        text=row["text"],
        received_at=row["received_at"],
        detected_language=Language(row["detected_language"]) if row["detected_language"] else None,
        english_translation=row["english_translation"],
        classification=Classification(row["classification"]) if row["classification"] else None,
        status=MessageStatus(row["status"]),
    )


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        message_id=row["message_id"],
        draft_text=row["draft_text"],
        created_at=row["created_at"],
        id=row["id"],
        draft_language=row["draft_language"],
        is_handoff=bool(row["is_handoff"]),
        slack_message_ts=row["slack_message_ts"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        sent_at=row["sent_at"],
        edited_text=row["edited_text"],
    )


# ---- messages ----


def insert_message(conn: sqlite3.Connection, message: Message) -> None:
    conn.execute(
        """INSERT INTO messages
               (id, platform, type, sender_id, sender_username, text,
                detected_language, english_translation, received_at,
                classification, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message.id,
            message.platform.value,
            message.type.value,
            message.sender_id,
            message.sender_username,
            message.text,
            message.detected_language.value if message.detected_language else None,
            message.english_translation,
            message.received_at,
            message.classification.value if message.classification else None,
            message.status.value,
        ),
    )
    conn.commit()


def get_message(conn: sqlite3.Connection, message_id: str) -> Optional[Message]:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _row_to_message(row) if row else None


def update_message_status(conn: sqlite3.Connection, message_id: str, status: MessageStatus) -> None:
    conn.execute("UPDATE messages SET status = ? WHERE id = ?", (status.value, message_id))
    conn.commit()


# ---- drafts ----


def insert_draft(conn: sqlite3.Connection, draft: Draft) -> int:
    cursor = conn.execute(
        """INSERT INTO drafts
               (message_id, draft_text, draft_language, is_handoff,
                slack_message_ts, created_at, approved_by, approved_at,
                sent_at, edited_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            draft.message_id,
            draft.draft_text,
            draft.draft_language,
            int(draft.is_handoff),
            draft.slack_message_ts,
            draft.created_at,
            draft.approved_by,
            draft.approved_at,
            draft.sent_at,
            draft.edited_text,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[Draft]:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return _row_to_draft(row) if row else None


def update_draft_slack_ts(conn: sqlite3.Connection, draft_id: int, ts: str) -> None:
    conn.execute("UPDATE drafts SET slack_message_ts = ? WHERE id = ?", (ts, draft_id))
    conn.commit()


def update_draft_approval(conn: sqlite3.Connection, draft_id: int, approved_by: str, approved_at: str) -> None:
    conn.execute(
        "UPDATE drafts SET approved_by = ?, approved_at = ? WHERE id = ?",
        (approved_by, approved_at, draft_id),
    )
    conn.commit()


def update_draft_sent(conn: sqlite3.Connection, draft_id: int, sent_at: str) -> None:
    conn.execute("UPDATE drafts SET sent_at = ? WHERE id = ?", (sent_at, draft_id))
    conn.commit()


def update_draft_edited_text(conn: sqlite3.Connection, draft_id: int, edited_text: str) -> None:
    conn.execute("UPDATE drafts SET edited_text = ? WHERE id = ?", (edited_text, draft_id))
    conn.commit()
