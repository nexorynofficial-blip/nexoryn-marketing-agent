"""Webhook-event dedup, echo detection, and draft expiry/nudge.

Webhook-event dedup (is_duplicate_webhook_event/mark_webhook_event_processed)
guards against Meta redelivering the same webhook event.

Echo detection cancels a pending draft when someone -- a teammate via Meta
Business Suite, or our own bot's already-approved send -- replied before
Slack approval happened. Primarily event-driven (near-zero extra API
calls): DMs use the messaging webhook's `is_echo` field, comments use the
new-comment webhook's `parent` field when the new comment is authored by
our own account (see webhooks.py). A periodic poll of
GET /{comment_id}/comments is a fallback safety net for comments only, in
case a reply's webhook event ever lacks a usable parent field.

In both the event-driven and polling paths, "someone already replied" is
detected the same way whether that someone was a human teammate or this
bot's own approved send -- the latter is a non-issue in practice because
the Approve flow already moves the message's status off PENDING_APPROVAL
*before* Meta echoes the send back to us, so by the time the echo/poll
runs there's nothing left to (incorrectly) cancel.

Draft expiry/nudge finds pending DM drafts approaching Meta's 24-hour
reply window and re-posts the approval card with a warning, reusing the
same Approve/Edit/Reject buttons.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import Set

from app.db import (
    get_connection,
    get_draft_for_message,
    get_message,
    get_pending_dm_message_by_sender,
    list_expiring_unnudged_dm_drafts,
    list_pending_comment_message_ids,
    update_draft_nudged,
    update_message_status,
)
from app.models import Message, MessageStatus
from app.pipeline import PipelineContext
from app.slack_app import post_expiry_nudge, update_message_as_already_handled

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---- webhook-event dedup ----


def is_duplicate_webhook_event(conn: sqlite3.Connection, webhook_event_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_webhook_ids WHERE webhook_event_id = ?", (webhook_event_id,)
    ).fetchone()
    return row is not None


def mark_webhook_event_processed(conn: sqlite3.Connection, webhook_event_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO processed_webhook_ids (webhook_event_id, received_at) VALUES (?, ?)",
        (webhook_event_id, _now_iso()),
    )
    conn.commit()


# ---- echo detection ----

_DM_ECHO_REASON = (
    "🔁 A reply was already sent to this person (by the team via Meta Business "
    "Suite) before this draft was approved."
)
_COMMENT_ECHO_REASON = (
    "🔁 A reply was already posted to this comment (by the team) before this draft was approved."
)


def _cancel_pending_draft(ctx: PipelineContext, message: Message, reason: str) -> None:
    conn = get_connection(ctx.db_path)
    try:
        draft = get_draft_for_message(conn, message.id)
        update_message_status(conn, message.id, MessageStatus.ALREADY_HANDLED)
    finally:
        conn.close()

    if draft is not None and draft.slack_message_ts:
        try:
            update_message_as_already_handled(
                ctx.slack_app,
                channel_id=ctx.slack_channel_id,
                ts=draft.slack_message_ts,
                message=message,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - the DB write already succeeded; a Slack hiccup shouldn't undo that
            logger.exception("echo_cancel_slack_update_failed", extra={"message_id": message.id})

    logger.info("draft_cancelled_by_echo", extra={"message_id": message.id, "reason": reason})


def handle_dm_echo(ctx: PipelineContext, recipient_id: str) -> None:
    """Called for a messaging webhook event with is_echo=True: the Page just
    sent a message to `recipient_id`. If that customer still has a
    PENDING_APPROVAL draft, someone beat Slack approval to it -- cancel it.
    """
    conn = get_connection(ctx.db_path)
    try:
        message = get_pending_dm_message_by_sender(conn, recipient_id)
    finally:
        conn.close()

    if message is None:
        return

    _cancel_pending_draft(ctx, message, reason=_DM_ECHO_REASON)


def handle_comment_echo(ctx: PipelineContext, parent_comment_id: str) -> None:
    """Called for a new comment authored by our own account that replies to
    `parent_comment_id`. If that original comment still has a
    PENDING_APPROVAL draft, cancel it."""
    conn = get_connection(ctx.db_path)
    try:
        message = get_message(conn, parent_comment_id)
    finally:
        conn.close()

    if message is None or message.status != MessageStatus.PENDING_APPROVAL:
        return

    _cancel_pending_draft(ctx, message, reason=_COMMENT_ECHO_REASON)


def run_comment_echo_poll(ctx: PipelineContext, own_ids: Set[str]) -> int:
    """Fallback safety net for comment echo detection, for cases where a
    reply's webhook event doesn't carry a usable parent field. Checks each
    still-pending comment for a reply from our own account. Non-blocking:
    a Graph API failure on one comment is logged and skipped, never raised,
    so one bad lookup can't stop the rest of the sweep or crash the job.
    """
    if ctx.meta is None:
        return 0

    conn = get_connection(ctx.db_path)
    try:
        comment_ids = list_pending_comment_message_ids(conn)
    finally:
        conn.close()

    cancelled = 0
    for comment_id in comment_ids:
        try:
            replies = ctx.meta.get_comment_replies(comment_id)
        except Exception:  # noqa: BLE001
            logger.exception("comment_echo_poll_failed", extra={"comment_id": comment_id})
            continue

        already_replied = any(reply.get("from", {}).get("id") in own_ids for reply in replies.get("data", []))
        if not already_replied:
            continue

        conn = get_connection(ctx.db_path)
        try:
            message = get_message(conn, comment_id)
        finally:
            conn.close()

        if message is not None and message.status == MessageStatus.PENDING_APPROVAL:
            _cancel_pending_draft(ctx, message, reason=_COMMENT_ECHO_REASON)
            cancelled += 1

    return cancelled


# ---- draft expiry / nudge ----


def _format_expiry_display(expires_at_iso: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(expires_at_iso)
    except ValueError:
        return expires_at_iso
    formatted = parsed.strftime("%I:%M %p")
    return formatted.lstrip("0") or formatted


def run_expiry_nudge_check(ctx: PipelineContext, within_hours: int = 4) -> int:
    """Finds pending DM drafts expiring within `within_hours` that haven't
    been nudged yet, and posts one nudge per draft."""
    cutoff = (dt.datetime.now() + dt.timedelta(hours=within_hours)).isoformat(timespec="seconds")

    conn = get_connection(ctx.db_path)
    try:
        expiring = list_expiring_unnudged_dm_drafts(conn, cutoff_iso=cutoff)
    finally:
        conn.close()

    nudged = 0
    for message, draft in expiring:
        try:
            post_expiry_nudge(
                ctx.slack_app,
                channel_id=ctx.slack_channel_id,
                db_path=ctx.db_path,
                message=message,
                draft_id=draft.id,
                expires_at_display=_format_expiry_display(draft.draft_expires_at),
            )
        except Exception:  # noqa: BLE001 - one failed nudge shouldn't block the rest
            logger.exception("expiry_nudge_failed", extra={"draft_id": draft.id})
            continue

        conn = get_connection(ctx.db_path)
        try:
            update_draft_nudged(conn, draft.id, _now_iso())
        finally:
            conn.close()
        nudged += 1

    if nudged:
        logger.info("expiry_nudges_posted", extra={"count": nudged})
    return nudged
