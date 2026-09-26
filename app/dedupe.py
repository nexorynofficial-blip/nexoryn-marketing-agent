"""Webhook-event dedup.

Meta sometimes retries a webhook delivery (e.g. if our endpoint is slow to
ack), so the same comment/message event can arrive more than once. These
two functions let webhooks.py skip anything already processed, keyed on a
per-item id (not the whole payload, since one POST can batch multiple
entries/changes/messaging items together).

Echo detection (human already replied via Meta Business Suite) and the
draft expiry/nudge background jobs are added here in Phase E.
"""
from __future__ import annotations

import datetime as dt
import sqlite3


def is_duplicate_webhook_event(conn: sqlite3.Connection, webhook_event_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_webhook_ids WHERE webhook_event_id = ?", (webhook_event_id,)
    ).fetchone()
    return row is not None


def mark_webhook_event_processed(conn: sqlite3.Connection, webhook_event_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO processed_webhook_ids (webhook_event_id, received_at) VALUES (?, ?)",
        (webhook_event_id, dt.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
