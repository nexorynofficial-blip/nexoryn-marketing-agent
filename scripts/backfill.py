"""Pulls comments and DMs from the last N hours that don't have a reply yet
and runs them through the full pipeline (classify -> draft/handoff/spam ->
Slack), exactly the way live webhook events are processed.

run_backfill(ctx, own_ids) is called from app.main on startup, before Slack
Socket Mode connects, so nothing races a live webhook event against a
backfilled one for the same message.

Run standalone (no server, just backfill once and exit):
    python scripts/backfill.py

NOTE: like webhooks.py, the exact Graph API shapes here (recent
posts/media -> their comments; conversations -> their messages) are built
from Meta's documented endpoints but not yet exercised against real
traffic -- verify once live testing is possible. Every post/media/
conversation/comment is handled in its own try/except so one bad item
never stops the rest of the sweep, and a message already in the DB (from
an earlier backfill run or a live webhook that beat this one) is skipped.
"""
from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Callable, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_connection, get_message
from app.models import Message, MessageStatus, MessageType, Platform
from app.pipeline import PipelineContext, process_message

logger = logging.getLogger(__name__)

DEFAULT_HOURS = 24
DEFAULT_LIMIT = 100


def run_backfill(
    ctx: PipelineContext, own_ids: Set[str], hours: int = DEFAULT_HOURS, limit: int = DEFAULT_LIMIT
) -> int:
    """Returns how many new messages were actually processed. Capped at
    `limit` total (comments + DMs combined) so a busy account can't make
    startup take too long."""
    if ctx.meta is None:
        logger.warning("backfill_skipped_no_meta_client")
        return 0

    cutoff = dt.datetime.now() - dt.timedelta(hours=hours)

    processed = _backfill_comments(ctx, cutoff, own_ids, limit)
    if processed < limit:
        processed += _backfill_dms(ctx, cutoff, own_ids, limit - processed)

    logger.info("backfill_complete", extra={"processed": processed, "hours": hours})
    return processed


# ---- comments ----


def _extract_comment_fields(comment: dict) -> Optional[dict]:
    comment_id = comment.get("id")
    text = comment.get("message") if "message" in comment else comment.get("text", "")
    created = comment.get("created_time") or comment.get("timestamp")
    sender = comment.get("from") or {}
    sender_id = sender.get("id")
    sender_username = sender.get("username") or sender.get("name") or comment.get("username")

    if not comment_id or not created:
        return None

    return {
        "comment_id": comment_id,
        "text": text,
        "created_time": created,
        "sender_id": sender_id,
        "sender_username": sender_username,
    }


def _process_comment_item(
    ctx: PipelineContext, comment: dict, platform: Platform, cutoff: dt.datetime, own_ids: Set[str]
) -> bool:
    fields = _extract_comment_fields(comment)
    if fields is None:
        return False

    if fields["sender_id"] is None:
        # Some comment listings (notably Instagram's, depending on
        # permissions) may omit `from.id`. Without a sender id we can't
        # safely build a Message row or filter our own replies, so skip
        # rather than guess.
        logger.warning("backfill_comment_missing_sender_id", extra={"comment_id": fields["comment_id"]})
        return False

    if fields["sender_id"] in own_ids:
        return False  # our own comment -- not something to draft a reply to

    created_at = _parse_meta_timestamp(fields["created_time"])
    if created_at is None or created_at < cutoff:
        return False

    conn = get_connection(ctx.db_path)
    try:
        if get_message(conn, fields["comment_id"]) is not None:
            return False  # already processed, live or an earlier backfill run
    finally:
        conn.close()

    message = Message(
        id=fields["comment_id"],
        platform=platform,
        type=MessageType.COMMENT,
        sender_id=fields["sender_id"],
        sender_username=fields["sender_username"],
        text=fields["text"],
        received_at=created_at.isoformat(timespec="seconds"),
        status=MessageStatus.PENDING_DRAFT,
    )
    process_message(ctx, message)
    return True


def _recent_container_ids(fetch: Callable[[], dict]) -> List[str]:
    try:
        result = fetch()
    except Exception:  # noqa: BLE001
        logger.exception("backfill_fetch_containers_failed")
        return []
    return [item["id"] for item in result.get("data", []) if "id" in item]


def _backfill_comments(ctx: PipelineContext, cutoff: dt.datetime, own_ids: Set[str], limit: int) -> int:
    processed = 0

    for post_id in _recent_container_ids(ctx.meta.get_recent_posts):
        if processed >= limit:
            return processed
        try:
            comments = ctx.meta.get_comments_for_post(post_id)
        except Exception:  # noqa: BLE001
            logger.exception("backfill_get_comments_failed", extra={"post_id": post_id})
            continue
        for comment in comments.get("data", []):
            if processed >= limit:
                return processed
            if _process_comment_item(ctx, comment, Platform.FACEBOOK, cutoff, own_ids):
                processed += 1

    for media_id in _recent_container_ids(ctx.meta.get_recent_media):
        if processed >= limit:
            return processed
        try:
            comments = ctx.meta.get_comments_for_media(media_id)
        except Exception:  # noqa: BLE001
            logger.exception("backfill_get_comments_failed", extra={"media_id": media_id})
            continue
        for comment in comments.get("data", []):
            if processed >= limit:
                return processed
            if _process_comment_item(ctx, comment, Platform.INSTAGRAM, cutoff, own_ids):
                processed += 1

    return processed


# ---- DMs ----


def _process_dm_conversation(
    ctx: PipelineContext, conversation_id: str, cutoff: dt.datetime, own_ids: Set[str]
) -> bool:
    try:
        result = ctx.meta.get_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("backfill_get_conversation_messages_failed", extra={"conversation_id": conversation_id})
        return False

    messages = result.get("data", [])
    if not messages:
        return False

    latest = messages[0]  # Graph API returns conversation messages newest-first
    sender = latest.get("from", {})
    sender_id = sender.get("id")
    mid = latest.get("id")

    if not mid or not sender_id or sender_id in own_ids:
        # Last message is ours (or malformed) -- conversation is already
        # answered, or there's nothing usable to backfill.
        return False

    created_at = _parse_meta_timestamp(latest.get("created_time"))
    if created_at is None or created_at < cutoff:
        return False

    conn = get_connection(ctx.db_path)
    try:
        if get_message(conn, mid) is not None:
            return False
    finally:
        conn.close()

    message = Message(
        id=mid,
        # The conversations endpoint doesn't reliably distinguish Messenger
        # from Instagram Direct in every API version; defaulting to
        # Instagram is a best-effort choice pending real-traffic verification.
        platform=Platform.INSTAGRAM,
        type=MessageType.DM,
        sender_id=sender_id,
        sender_username=None,
        text=latest.get("message", ""),
        received_at=created_at.isoformat(timespec="seconds"),
        status=MessageStatus.PENDING_DRAFT,
    )
    process_message(ctx, message)
    return True


def _backfill_dms(ctx: PipelineContext, cutoff: dt.datetime, own_ids: Set[str], limit: int) -> int:
    try:
        conversations = ctx.meta.get_conversations()
    except Exception:  # noqa: BLE001
        logger.exception("backfill_get_conversations_failed")
        return 0

    processed = 0
    for conv in conversations.get("data", []):
        if processed >= limit:
            return processed
        conv_id = conv.get("id")
        if not conv_id:
            continue
        if _process_dm_conversation(ctx, conv_id, cutoff, own_ids):
            processed += 1

    return processed


def _parse_meta_timestamp(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        logger.warning("backfill_unparsable_timestamp", extra={"value": value})
        return None
    # Compared against a naive `cutoff`, consistent with received_at being
    # stored as a naive local-time ISO string everywhere else in this app.
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


if __name__ == "__main__":
    import argparse

    from app.claude_client import ClaudeClient
    from app.config import load_config
    from app.db import init_db
    from app.knowledge import load_knowledge
    from app.meta_client import MetaClient
    from app.slack_app import SendCallbacks, build_slack_app

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Backfill unanswered comments/DMs from the last N hours.")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    config = load_config()
    init_db(config.db_path)

    knowledge = load_knowledge()
    claude = ClaudeClient(api_key=config.anthropic_api_key, knowledge=knowledge)
    meta = MetaClient.from_config(config)

    send_callbacks = SendCallbacks(
        send_comment_reply=lambda comment_id, text: meta.reply_to_comment(comment_id, text),
        send_dm_reply=lambda recipient_id, text: meta.send_dm(recipient_id, text),
        hide_comment=lambda comment_id: meta.hide_comment(comment_id, hide=True),
    )
    slack_app = build_slack_app(bot_token=config.slack_bot_token, db_path=config.db_path, send_callbacks=send_callbacks)

    ctx = PipelineContext(
        db_path=config.db_path,
        slack_app=slack_app,
        slack_channel_id=config.slack_approvals_channel_id,
        claude=claude,
        knowledge=knowledge,
        meta=meta,
    )
    own_ids = {config.meta_page_id, config.meta_ig_business_id}

    count = run_backfill(ctx, own_ids, hours=args.hours, limit=args.limit)
    print(f"Backfilled {count} message(s) from the last {args.hours}h.")
