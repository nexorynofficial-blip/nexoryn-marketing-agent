"""SQLite connection and schema management.

Single-tenant, local-first app: one file, no external database service.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
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
    status TEXT NOT NULL DEFAULT 'pending_draft' CHECK (status IN (
        'pending_draft', 'pending_approval', 'approved', 'sent',
        'rejected', 'expired', 'already_handled', 'spam_pending',
        'spam_hidden', 'spam_kept'
    ))
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
    sent_at TEXT
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


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
