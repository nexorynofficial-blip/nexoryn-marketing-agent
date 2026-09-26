"""Core pipeline: incoming Meta event -> classify -> draft (or
handoff/alert-only/spam-check) -> post to Slack for approval.

This module never sends anything to Meta directly -- that only happens
after a human clicks Approve in Slack (see slack_app.SendCallbacks). It
owns exactly the one-way "customer message in -> Slack card out" flow,
including the escalation rules from agency_knowledge.md that the fixed
classification categories alone can't express:

  - Spam comments skip drafting entirely and go straight to Hide/Keep.
    Spam DMs can't be "hidden", so they fall back to the normal handoff
    draft flow instead.
  - Pricing questions get the fixed FAQ pricing line prepended to the
    handoff message (in any language -- see ClaudeClient.classify_message's
    `mentions_pricing` field, since a hardcoded keyword list would miss
    Urdu-script/Roman-Urdu pricing questions).
  - Comments that end up needing the team (pricing, meetings, or anything
    the model can't answer from the knowledge doc) get NO public reply at
    all, only a Slack alert -- only DMs get the handoff message. This is
    checked *after* drafting, since the model can also self-escalate mid-
    draft (see claude_client's HANDOFF_NEEDED sentinel) for situations the
    classification step alone didn't anticipate.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import List, Optional

from slack_bolt import App

from app.claude_client import ClaudeClient, ConversationTurn
from app.db import get_connection, get_message, insert_draft, insert_message, update_message_status
from app.knowledge import Knowledge
from app.meta_client import MetaClient
from app.models import Classification, Draft, Language, Message, MessageStatus, MessageType, Platform
from app.slack_app import post_alert_only, post_draft_for_approval, post_spam_check

logger = logging.getLogger(__name__)


@dataclass
class PipelineContext:
    db_path: str
    slack_app: App
    slack_channel_id: str
    claude: ClaudeClient
    knowledge: Knowledge
    meta: Optional[MetaClient] = None  # None in tests that don't need conversation history


@dataclass(frozen=True)
class CommentEvent:
    dedup_key: str
    comment_id: str
    platform: Platform
    sender_id: str
    sender_username: Optional[str]
    text: str


@dataclass(frozen=True)
class MessageEvent:
    dedup_key: str
    message_id: str
    platform: Platform
    sender_id: str
    text: str


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def process_comment_event(ctx: PipelineContext, event: CommentEvent) -> None:
    message = Message(
        id=event.comment_id,
        platform=event.platform,
        type=MessageType.COMMENT,
        sender_id=event.sender_id,
        sender_username=event.sender_username,
        text=event.text,
        received_at=_now_iso(),
        status=MessageStatus.PENDING_DRAFT,
    )
    _process_message(ctx, message)


def process_message_event(ctx: PipelineContext, event: MessageEvent) -> None:
    message = Message(
        id=event.message_id,
        platform=event.platform,
        type=MessageType.DM,
        sender_id=event.sender_id,
        sender_username=None,
        text=event.text,
        received_at=_now_iso(),
        status=MessageStatus.PENDING_DRAFT,
    )
    _process_message(ctx, message)


def _fetch_conversation_history(ctx: PipelineContext, message: Message) -> Optional[List[ConversationTurn]]:
    """Best-effort DM history for extra drafting context.

    NOT YET VERIFIED against real Meta webhook traffic -- deliberately a
    no-op stub rather than a guessed Graph API call, since getting this
    wrong would look like it works while quietly returning nothing useful.
    Revisit once live DM webhooks are flowing (Phase D handoff notes).
    """
    return None


def _process_message(ctx: PipelineContext, message: Message) -> None:
    conn = get_connection(ctx.db_path)
    try:
        if get_message(conn, message.id) is not None:
            logger.info("message_already_processed", extra={"message_id": message.id})
            return

        insert_message(conn, message)

        result = ctx.claude.classify_message(message.text)
        message.classification = result.category
        message.detected_language = result.language

        message_translation = None
        if result.language != Language.ENGLISH:
            try:
                message_translation = ctx.claude.translate_to_english(message.text)
                message.english_translation = message_translation
            except Exception:  # noqa: BLE001 - translation is a display nicety, never block the pipeline
                logger.exception("message_translation_failed", extra={"message_id": message.id})

        conn.execute(
            "UPDATE messages SET classification = ?, detected_language = ?, english_translation = ? WHERE id = ?",
            (message.classification.value, message.detected_language.value, message.english_translation, message.id),
        )
        conn.commit()

        # Spam comments: no draft at all, straight to Hide/Keep. Spam DMs
        # can't be "hidden" via the Graph API, so they fall through to the
        # normal handoff-draft path below instead.
        if result.category == Classification.SPAM and message.type == MessageType.COMMENT:
            update_message_status(conn, message.id, MessageStatus.SPAM_PENDING)
            post_spam_check(ctx.slack_app, channel_id=ctx.slack_channel_id, db_path=ctx.db_path, message=message)
            return

        is_handoff = (
            result.mentions_pricing
            or result.category == Classification.LEAD
            or (result.category == Classification.SPAM and message.type == MessageType.DM)
        )

        conversation_history = None
        if message.type == MessageType.DM:
            conversation_history = _fetch_conversation_history(ctx, message)

        draft_result = ctx.claude.draft_reply(
            message_text=message.text,
            classification=result.category,
            language=result.language,
            is_handoff=is_handoff,
            conversation_history=conversation_history,
        )

        draft_text = draft_result.text
        if draft_result.is_handoff and result.mentions_pricing:
            draft_text = f"{ctx.knowledge.pricing_line}\n\n{draft_text}"

        if draft_result.is_handoff and message.type == MessageType.COMMENT:
            # "Comments that need the team get no public reply at all --
            # only DMs get the handoff message" (agency_knowledge.md).
            update_message_status(conn, message.id, MessageStatus.ALERTED)
            post_alert_only(
                ctx.slack_app,
                channel_id=ctx.slack_channel_id,
                message=message,
                message_translation=message_translation,
            )
            return

        draft_translation = None
        if result.language != Language.ENGLISH:
            try:
                draft_translation = ctx.claude.translate_to_english(draft_text)
            except Exception:  # noqa: BLE001
                logger.exception("draft_translation_failed", extra={"message_id": message.id})

        draft = Draft(
            message_id=message.id,
            draft_text=draft_text,
            created_at=_now_iso(),
            draft_language=result.language.value,
            is_handoff=draft_result.is_handoff,
        )
        draft_id = insert_draft(conn, draft)
        update_message_status(conn, message.id, MessageStatus.PENDING_APPROVAL)

        post_draft_for_approval(
            ctx.slack_app,
            channel_id=ctx.slack_channel_id,
            db_path=ctx.db_path,
            message=message,
            draft_id=draft_id,
            message_translation=message_translation,
            draft_translation=draft_translation,
        )
    finally:
        conn.close()
