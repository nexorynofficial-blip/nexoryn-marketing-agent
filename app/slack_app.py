"""Slack Bolt app: posts drafts for approval, handles Approve/Edit/Reject
(and Hide/Keep for spam) button actions, the Edit modal, and updates the
Slack message in place after any action.

Sending the approved text to Meta is deliberately NOT this module's job --
it happens through the `SendCallbacks` object passed in at construction, so
Phase C can be tested with a stub callback (no live Graph API calls) and
Phase D's pipeline.py can later register real MetaClient-backed callbacks
without slack_app.py changing.

Double-action protection: every handler re-reads the message's status from
SQLite before acting. Since a Slack button click only ever reaches one
handler invocation at a time per message and SQLite serializes writes, the
first click to land wins; a second click sees a non-pending status and gets
a "someone already handled this" ephemeral instead of double-sending.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from slack_bolt import App

from app.db import (
    get_connection,
    get_draft,
    get_message,
    update_draft_approval,
    update_draft_edited_text,
    update_draft_sent,
    update_draft_slack_ts,
    update_message_status,
)
from app.models import Draft, Message, MessageStatus, MessageType, Platform

logger = logging.getLogger(__name__)

_PLATFORM_ICON = {Platform.INSTAGRAM: "📷", Platform.FACEBOOK: "📘"}
_TYPE_LABEL = {MessageType.COMMENT: "Comment", MessageType.DM: "DM"}


@dataclass
class SendCallbacks:
    """Hooks slack_app.py calls after a human acts on a draft.

    Each callback should raise on failure (any exception) rather than
    return a status code -- the handlers catch it, log it, and surface a
    clear failure state in Slack instead of silently marking as sent.
    """

    send_comment_reply: Callable[[str, str], None]  # (comment_id, text) -> None
    send_dm_reply: Callable[[str, str], None]  # (recipient_id, text) -> None
    hide_comment: Callable[[str], None]  # (comment_id) -> None


def build_slack_app(*, bot_token: str, db_path: str, send_callbacks: SendCallbacks) -> App:
    app = App(token=bot_token)
    _register_listeners(app, db_path=db_path, send_callbacks=send_callbacks)
    return app


# ---- posting new drafts/spam-checks ----


def post_draft_for_approval(
    app: App,
    *,
    channel_id: str,
    db_path: str,
    message: Message,
    draft_id: int,
    message_translation: Optional[str] = None,
    draft_translation: Optional[str] = None,
) -> str:
    """Posts the initial approval message for an already-inserted message +
    draft pair, and stores the resulting Slack ts back onto the draft row."""
    conn = get_connection(db_path)
    try:
        draft = get_draft(conn, draft_id)
        if draft is None:
            raise ValueError(f"No draft row with id={draft_id}")

        blocks = _build_pending_blocks(message, draft, message_translation, draft_translation)
        result = app.client.chat_postMessage(
            channel=channel_id,
            blocks=blocks,
            text=f"New draft for approval: {message.text[:100]}",
        )
        ts = result["ts"]
        update_draft_slack_ts(conn, draft_id, ts)
        return ts
    finally:
        conn.close()


def post_spam_check(app: App, *, channel_id: str, db_path: str, message: Message) -> str:
    """Posts a spam-check message with only Hide/Keep buttons -- no draft
    text, per the knowledge doc's spam guidance."""
    conn = get_connection(db_path)
    try:
        blocks = _build_spam_blocks(message)
        result = app.client.chat_postMessage(
            channel=channel_id,
            blocks=blocks,
            text=f"Possible spam: {message.text[:100]}",
        )
        return result["ts"]
    finally:
        conn.close()


def post_alert_only(
    app: App,
    *,
    channel_id: str,
    message: Message,
    message_translation: Optional[str] = None,
) -> str:
    """Posts a plain notification with no buttons at all -- used for comments
    that need the team (pricing/meetings/off-doc questions), which per
    agency_knowledge.md get NO public reply, only an internal alert. There's
    nothing to approve/reject here, so no draft row and no DB write."""
    blocks = _build_alert_only_blocks(message, message_translation)
    result = app.client.chat_postMessage(
        channel=channel_id,
        blocks=blocks,
        text=f"Needs a personal reply: {message.text[:100]}",
    )
    return result["ts"]


# ---- block builders (pure, easy to unit test) ----


def _label(message: Message) -> str:
    icon = _PLATFORM_ICON.get(message.platform, "")
    platform_name = message.platform.value.capitalize()
    return f"{icon} {platform_name} {_TYPE_LABEL[message.type]}"


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _now_display() -> str:
    """Server-local time, cross-platform (avoids %-I/%#I strftime flags,
    which only exist on one platform each)."""
    formatted = dt.datetime.now().strftime("%I:%M %p")
    return formatted.lstrip("0") or formatted


def _build_pending_blocks(
    message: Message,
    draft: Draft,
    message_translation: Optional[str] = None,
    draft_translation: Optional[str] = None,
) -> List[dict]:
    blocks: List[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": _label(message), "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n@{message.sender_username or message.sender_id}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Classification:*\n{message.classification.value if message.classification else 'unknown'}",
                },
            ],
        },
        {
            "type": "section",
            "block_id": "message_text_block",
            "text": {"type": "mrkdwn", "text": f"*Message:*\n{message.text}"},
        },
    ]

    if message_translation:
        blocks.append(
            {
                "type": "context",
                "block_id": "message_translation_block",
                "elements": [{"type": "mrkdwn", "text": f"*English:* {message_translation}"}],
            }
        )

    blocks.append({"type": "divider"})

    display_text = draft.edited_text or draft.draft_text
    label = "Draft reply (handoff)" if draft.is_handoff else "Draft reply"
    blocks.append(
        {
            "type": "section",
            "block_id": "draft_text_block",
            "text": {"type": "mrkdwn", "text": f"*{label}:*\n{display_text}"},
        }
    )

    if draft_translation:
        blocks.append(
            {
                "type": "context",
                "block_id": "draft_translation_block",
                "elements": [{"type": "mrkdwn", "text": f"*English:* {draft_translation}"}],
            }
        )

    value = json.dumps({"message_id": message.id, "draft_id": draft.id})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "approve_draft",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit", "emoji": True},
                    "action_id": "edit_draft",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "reject_draft",
                    "value": value,
                },
            ],
        }
    )
    return blocks


def _build_spam_blocks(message: Message) -> List[dict]:
    blocks: List[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚩 Possible spam — {_label(message)}", "emoji": True},
        },
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f"*From:*\n@{message.sender_username or message.sender_id}"}],
        },
        {
            "type": "section",
            "block_id": "message_text_block",
            "text": {"type": "mrkdwn", "text": f"*Message:*\n{message.text}"},
        },
        {"type": "divider"},
    ]

    value = json.dumps({"message_id": message.id})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Hide", "emoji": True},
                    "style": "danger",
                    "action_id": "hide_comment",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Keep", "emoji": True},
                    "action_id": "keep_comment",
                    "value": value,
                },
            ],
        }
    )
    return blocks


def _build_alert_only_blocks(message: Message, message_translation: Optional[str] = None) -> List[dict]:
    blocks: List[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🔔 Needs a personal reply — {_label(message)}", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n@{message.sender_username or message.sender_id}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Classification:*\n{message.classification.value if message.classification else 'unknown'}",
                },
            ],
        },
        {
            "type": "section",
            "block_id": "message_text_block",
            "text": {"type": "mrkdwn", "text": f"*Message:*\n{message.text}"},
        },
    ]

    if message_translation:
        blocks.append(
            {
                "type": "context",
                "block_id": "message_translation_block",
                "elements": [{"type": "mrkdwn", "text": f"*English:* {message_translation}"}],
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "No public reply was drafted — this needs a personal reply from the team directly on Instagram/Facebook.",
                }
            ],
        }
    )
    return blocks


def _replace_actions_with_resolution(blocks: List[dict], resolution_line: str) -> List[dict]:
    kept = [b for b in blocks if b.get("type") != "actions"]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": resolution_line}]})
    return kept


def _apply_edit_to_blocks(blocks: List[dict], edited_text: str) -> List[dict]:
    """Swaps the draft-reply section for the human's edited text and drops
    the now-stale draft translation, leaving everything else untouched."""
    new_blocks: List[dict] = []
    for block in blocks:
        if block.get("block_id") == "draft_translation_block":
            continue
        if block.get("block_id") == "draft_text_block":
            new_blocks.append(
                {
                    "type": "section",
                    "block_id": "draft_text_block",
                    "text": {"type": "mrkdwn", "text": f"*Draft reply (edited):*\n{edited_text}"},
                }
            )
            continue
        new_blocks.append(block)
    return new_blocks


def _build_edit_modal(private_metadata: str, initial_text: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "edit_draft_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Edit reply"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "edited_text_block",
                "label": {"type": "plain_text", "text": "Reply text"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edited_text_input",
                    "multiline": True,
                    "initial_value": initial_text,
                },
            }
        ],
    }


# ---- listener registration ----


def _register_listeners(app: App, *, db_path: str, send_callbacks: SendCallbacks) -> None:
    def guard_still_pending(client, body, message_id: str, expected_status: MessageStatus, conn) -> Optional[Message]:
        message = get_message(conn, message_id)
        if message is None or message.status != expected_status:
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="Someone already acted on this one — refresh to see the latest state.",
            )
            return None
        return message

    def send_reply(message: Message, text: str) -> None:
        if message.type == MessageType.COMMENT:
            send_callbacks.send_comment_reply(message.id, text)
        else:
            send_callbacks.send_dm_reply(message.sender_id, text)

    @app.action("approve_draft")
    def handle_approve(ack, body, client):
        ack()
        payload = json.loads(body["actions"][0]["value"])
        message_id, draft_id = payload["message_id"], payload["draft_id"]
        actor = f"<@{body['user']['id']}>"

        conn = get_connection(db_path)
        try:
            message = guard_still_pending(client, body, message_id, MessageStatus.PENDING_APPROVAL, conn)
            if message is None:
                return
            draft = get_draft(conn, draft_id)
            text_to_send = draft.edited_text or draft.draft_text
            now = _now_iso()

            update_draft_approval(conn, draft_id, approved_by=body["user"]["id"], approved_at=now)
            update_message_status(conn, message_id, MessageStatus.APPROVED)

            try:
                send_reply(message, text_to_send)
            except Exception as exc:  # noqa: BLE001 - surface any send failure in Slack, don't crash the handler
                logger.error("send_failed", extra={"message_id": message_id, "error": str(exc)})
                resolution = f"⚠️ Approved by {actor} at {_now_display()} but SENDING FAILED: {exc}"
            else:
                update_draft_sent(conn, draft_id, sent_at=now)
                update_message_status(conn, message_id, MessageStatus.SENT)
                resolution = f"✅ Approved by {actor} at {_now_display()}"

            blocks = _replace_actions_with_resolution(body["message"]["blocks"], resolution)
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=blocks, text=resolution)
        finally:
            conn.close()

    @app.action("reject_draft")
    def handle_reject(ack, body, client):
        ack()
        payload = json.loads(body["actions"][0]["value"])
        message_id = payload["message_id"]
        actor = f"<@{body['user']['id']}>"

        conn = get_connection(db_path)
        try:
            message = guard_still_pending(client, body, message_id, MessageStatus.PENDING_APPROVAL, conn)
            if message is None:
                return
            update_message_status(conn, message_id, MessageStatus.REJECTED)
            resolution = f"❌ Rejected by {actor} at {_now_display()}"
            blocks = _replace_actions_with_resolution(body["message"]["blocks"], resolution)
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=blocks, text=resolution)
        finally:
            conn.close()

    @app.action("edit_draft")
    def handle_edit_click(ack, body, client):
        ack()
        payload = json.loads(body["actions"][0]["value"])
        message_id, draft_id = payload["message_id"], payload["draft_id"]

        conn = get_connection(db_path)
        try:
            message = guard_still_pending(client, body, message_id, MessageStatus.PENDING_APPROVAL, conn)
            if message is None:
                return
            draft = get_draft(conn, draft_id)
        finally:
            conn.close()

        current_text = draft.edited_text or draft.draft_text
        # display_blocks captures the already-rendered message (including any
        # translation context) so the final Slack update after submit doesn't
        # need to re-derive translations, which aren't persisted in SQLite.
        metadata = json.dumps(
            {
                "channel_id": body["channel"]["id"],
                "message_ts": body["message"]["ts"],
                "message_id": message_id,
                "draft_id": draft_id,
                "display_blocks": [b for b in body["message"]["blocks"] if b.get("type") != "actions"],
            }
        )
        client.views_open(trigger_id=body["trigger_id"], view=_build_edit_modal(metadata, current_text))

    @app.view("edit_draft_modal")
    def handle_edit_submit(ack, body, client):
        ack()
        metadata = json.loads(body["view"]["private_metadata"])
        edited_text = body["view"]["state"]["values"]["edited_text_block"]["edited_text_input"]["value"]
        message_id, draft_id = metadata["message_id"], metadata["draft_id"]
        actor = f"<@{body['user']['id']}>"

        conn = get_connection(db_path)
        try:
            message = get_message(conn, message_id)
            if message is None or message.status != MessageStatus.PENDING_APPROVAL:
                client.chat_postEphemeral(
                    channel=metadata["channel_id"],
                    user=body["user"]["id"],
                    text="Someone already acted on this one before your edit was submitted.",
                )
                return

            now = _now_iso()
            update_draft_edited_text(conn, draft_id, edited_text)
            update_draft_approval(conn, draft_id, approved_by=body["user"]["id"], approved_at=now)
            update_message_status(conn, message_id, MessageStatus.APPROVED)

            try:
                send_reply(message, edited_text)
            except Exception as exc:  # noqa: BLE001
                logger.error("send_failed", extra={"message_id": message_id, "error": str(exc)})
                resolution = f"⚠️ Edited & approved by {actor} at {_now_display()} but SENDING FAILED: {exc}"
            else:
                update_draft_sent(conn, draft_id, sent_at=now)
                update_message_status(conn, message_id, MessageStatus.SENT)
                resolution = f"✏️ Edited & approved by {actor} at {_now_display()}"

            blocks = _apply_edit_to_blocks(metadata["display_blocks"], edited_text)
            blocks = _replace_actions_with_resolution(blocks, resolution)
            client.chat_update(channel=metadata["channel_id"], ts=metadata["message_ts"], blocks=blocks, text=resolution)
        finally:
            conn.close()

    @app.action("hide_comment")
    def handle_hide(ack, body, client):
        ack()
        payload = json.loads(body["actions"][0]["value"])
        message_id = payload["message_id"]
        actor = f"<@{body['user']['id']}>"

        conn = get_connection(db_path)
        try:
            message = guard_still_pending(client, body, message_id, MessageStatus.SPAM_PENDING, conn)
            if message is None:
                return

            try:
                send_callbacks.hide_comment(message.id)
            except Exception as exc:  # noqa: BLE001
                logger.error("hide_failed", extra={"message_id": message_id, "error": str(exc)})
                resolution = f"⚠️ {actor} clicked Hide at {_now_display()} but hiding FAILED: {exc}"
            else:
                update_message_status(conn, message_id, MessageStatus.SPAM_HIDDEN)
                resolution = f"🚫 Hidden by {actor} at {_now_display()}"

            blocks = _replace_actions_with_resolution(body["message"]["blocks"], resolution)
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=blocks, text=resolution)
        finally:
            conn.close()

    @app.action("keep_comment")
    def handle_keep(ack, body, client):
        ack()
        payload = json.loads(body["actions"][0]["value"])
        message_id = payload["message_id"]
        actor = f"<@{body['user']['id']}>"

        conn = get_connection(db_path)
        try:
            message = guard_still_pending(client, body, message_id, MessageStatus.SPAM_PENDING, conn)
            if message is None:
                return
            update_message_status(conn, message_id, MessageStatus.SPAM_KEPT)
            resolution = f"✅ Kept by {actor} at {_now_display()}"
            blocks = _replace_actions_with_resolution(body["message"]["blocks"], resolution)
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=blocks, text=resolution)
        finally:
            conn.close()
