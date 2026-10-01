"""SQLite connection, schema, and row<->dataclass helpers for the CLI.

Single local file, no external database -- this is an on-demand CLI tool,
not a server. `summaries` stores each run's full output as JSON (plus a
few indexed columns for the `status` command's history list) rather than
a fully normalized schema, since nothing needs to query inside a past
summary's details beyond what the CLI already displays.
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from app.models import Comment, Message, Post, Summary

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    message TEXT,
    created_time TEXT NOT NULL,
    reactions_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    permalink_url TEXT,
    engagement_score INTEGER,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES posts(id),
    text TEXT,
    author_name TEXT,
    author_id TEXT,
    created_time TEXT NOT NULL,
    has_page_reply INTEGER NOT NULL DEFAULT 0,
    sentiment TEXT,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT,
    text TEXT,
    created_time TEXT NOT NULL,
    is_from_page INTEGER NOT NULL DEFAULT 0,
    theme TEXT,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL,
    total_action_items INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_post_id ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_summaries_timestamp ON summaries(timestamp);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Table names from this project's earlier FastAPI/Slack-approval
# architecture (now removed). That schema's own `messages` table means
# something completely different from this one's (IG/FB comment-and-DM
# approval records vs. engagement-analysis DM records), so it's dropped
# and recreated fresh below rather than migrated -- there's nothing in it
# worth preserving for this tool's purpose.
_OLD_ARCHITECTURE_ONLY_TABLES = ["drafts", "processed_webhook_ids"]


def _messages_table_is_old_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'").fetchone()
    if row is None:
        return False
    return "conversation_id" not in row["sql"]


def _drop_old_architecture_tables(conn: sqlite3.Connection) -> None:
    # drafts.message_id REFERENCES messages(id) in the old schema, so it
    # must go first -- dropping messages while drafts still references it
    # violates the FK constraint.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for table in _OLD_ARCHITECTURE_ONLY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        if _messages_table_is_old_schema(conn):
            conn.execute("DROP TABLE IF EXISTS messages")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        _drop_old_architecture_tables(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---- row <-> dataclass mapping ----


def _row_to_post(row: sqlite3.Row) -> Post:
    return Post(
        id=row["id"],
        message=row["message"],
        created_time=row["created_time"],
        reactions_count=row["reactions_count"],
        comments_count=row["comments_count"],
        permalink_url=row["permalink_url"],
        engagement_score=row["engagement_score"],
        fetched_at=row["fetched_at"],
    )


def _row_to_comment(row: sqlite3.Row) -> Comment:
    return Comment(
        id=row["id"],
        post_id=row["post_id"],
        text=row["text"],
        author_name=row["author_name"],
        author_id=row["author_id"],
        created_time=row["created_time"],
        has_page_reply=bool(row["has_page_reply"]),
        sentiment=row["sentiment"],
        fetched_at=row["fetched_at"],
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        sender_id=row["sender_id"],
        sender_name=row["sender_name"],
        text=row["text"],
        created_time=row["created_time"],
        is_from_page=bool(row["is_from_page"]),
        theme=row["theme"],
        fetched_at=row["fetched_at"],
    )


# ---- posts ----


def store_posts(conn: sqlite3.Connection, posts: List[Post], now: str) -> None:
    for post in posts:
        conn.execute(
            """INSERT INTO posts
                   (id, message, created_time, reactions_count, comments_count,
                    permalink_url, engagement_score, fetched_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   message = excluded.message,
                   created_time = excluded.created_time,
                   reactions_count = excluded.reactions_count,
                   comments_count = excluded.comments_count,
                   permalink_url = excluded.permalink_url,
                   engagement_score = excluded.engagement_score,
                   fetched_at = excluded.fetched_at,
                   updated_at = excluded.updated_at""",
            (
                post.id,
                post.message,
                post.created_time,
                post.reactions_count,
                post.comments_count,
                post.permalink_url,
                post.engagement_score,
                post.fetched_at,
                now,
                now,
            ),
        )
    conn.commit()


def get_post(conn: sqlite3.Connection, post_id: str) -> Optional[Post]:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return _row_to_post(row) if row else None


def get_posts_since(conn: sqlite3.Connection, cutoff_iso: str) -> List[Post]:
    rows = conn.execute(
        "SELECT * FROM posts WHERE created_time >= ? ORDER BY created_time DESC", (cutoff_iso,)
    ).fetchall()
    return [_row_to_post(r) for r in rows]


# ---- comments ----


def store_comments(conn: sqlite3.Connection, comments: List[Comment], now: str) -> None:
    for comment in comments:
        conn.execute(
            """INSERT INTO comments
                   (id, post_id, text, author_name, author_id, created_time,
                    has_page_reply, sentiment, fetched_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   text = excluded.text,
                   author_name = excluded.author_name,
                   author_id = excluded.author_id,
                   created_time = excluded.created_time,
                   has_page_reply = excluded.has_page_reply,
                   sentiment = COALESCE(excluded.sentiment, comments.sentiment),
                   fetched_at = excluded.fetched_at,
                   updated_at = excluded.updated_at""",
            (
                comment.id,
                comment.post_id,
                comment.text,
                comment.author_name,
                comment.author_id,
                comment.created_time,
                int(comment.has_page_reply),
                comment.sentiment,
                comment.fetched_at,
                now,
                now,
            ),
        )
    conn.commit()


def update_comment_sentiment(conn: sqlite3.Connection, comment_id: str, sentiment: str) -> None:
    conn.execute("UPDATE comments SET sentiment = ? WHERE id = ?", (sentiment, comment_id))
    conn.commit()


def get_unanswered_comments(conn: sqlite3.Connection, cutoff_iso: str) -> List[Comment]:
    rows = conn.execute(
        """SELECT * FROM comments
           WHERE created_time >= ? AND has_page_reply = 0
           ORDER BY created_time DESC""",
        (cutoff_iso,),
    ).fetchall()
    return [_row_to_comment(r) for r in rows]


def get_comments_for_post(conn: sqlite3.Connection, post_id: str) -> List[Comment]:
    rows = conn.execute(
        "SELECT * FROM comments WHERE post_id = ? ORDER BY created_time DESC", (post_id,)
    ).fetchall()
    return [_row_to_comment(r) for r in rows]


# ---- messages ----


def store_messages(conn: sqlite3.Connection, messages: List[Message], now: str) -> None:
    for message in messages:
        conn.execute(
            """INSERT INTO messages
                   (id, conversation_id, sender_id, sender_name, text, created_time,
                    is_from_page, theme, fetched_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   sender_name = excluded.sender_name,
                   text = excluded.text,
                   created_time = excluded.created_time,
                   is_from_page = excluded.is_from_page,
                   theme = COALESCE(excluded.theme, messages.theme),
                   fetched_at = excluded.fetched_at,
                   updated_at = excluded.updated_at""",
            (
                message.id,
                message.conversation_id,
                message.sender_id,
                message.sender_name,
                message.text,
                message.created_time,
                int(message.is_from_page),
                message.theme,
                message.fetched_at,
                now,
                now,
            ),
        )
    conn.commit()


def update_message_theme(conn: sqlite3.Connection, message_id: str, theme: str) -> None:
    conn.execute("UPDATE messages SET theme = ? WHERE id = ?", (theme, message_id))
    conn.commit()


def get_messages_since(conn: sqlite3.Connection, cutoff_iso: str) -> List[Message]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE created_time >= ? ORDER BY created_time DESC", (cutoff_iso,)
    ).fetchall()
    return [_row_to_message(r) for r in rows]


# ---- summaries ----


def store_summary(conn: sqlite3.Connection, summary: Summary, now: str) -> None:
    conn.execute(
        """INSERT INTO summaries (timestamp, status, total_action_items, data_json, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (summary.timestamp, summary.status, summary.total_action_items, summary.model_dump_json(), now),
    )
    conn.commit()


def get_last_summary(conn: sqlite3.Connection) -> Optional[Summary]:
    row = conn.execute("SELECT data_json FROM summaries ORDER BY id DESC LIMIT 1").fetchone()
    return Summary.model_validate_json(row["data_json"]) if row else None


def get_recent_summaries(conn: sqlite3.Connection, limit: int = 5) -> List[Summary]:
    rows = conn.execute("SELECT data_json FROM summaries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [Summary.model_validate_json(r["data_json"]) for r in rows]
