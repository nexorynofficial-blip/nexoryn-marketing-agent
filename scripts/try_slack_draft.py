"""Manual test for Phase C.

Posts a fake normal draft and a fake spam item to the approvals channel,
then starts the Slack app in Socket Mode so you can click the buttons live
and watch the DB + Slack message update in real time.

Uses stub send callbacks that just print what they *would* send -- no real
Meta Graph API calls happen here. Phase D wires the real MetaClient-backed
callbacks into pipeline.py.

Run:
    python scripts/try_slack_draft.py

Then open the approvals channel in Slack:
  - On the first message: click Approve, Edit, or Reject.
  - On the second (spam) message: click Hide or Keep.
Watch the terminal for the stub "would send" output, and check that each
Slack message updates in place with who acted and when, buttons removed.

Press Ctrl+C to stop.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.config import load_config
from app.db import get_connection, init_db, insert_draft, insert_message
from app.models import Classification, Draft, Language, Message, MessageStatus, MessageType, Platform
from app.slack_app import SendCallbacks, build_slack_app, post_draft_for_approval, post_spam_check


def fake_send_comment_reply(comment_id: str, text: str) -> None:
    print(f"[stub] Would reply to comment {comment_id!r} with: {text!r}")


def fake_send_dm_reply(recipient_id: str, text: str) -> None:
    print(f"[stub] Would send DM to {recipient_id!r}: {text!r}")


def fake_hide_comment(comment_id: str) -> None:
    print(f"[stub] Would hide comment {comment_id!r}")


def seed_normal_draft(conn, run_id: str) -> tuple[Message, int]:
    now = dt.datetime.now().isoformat(timespec="seconds")
    message = Message(
        id=f"test-comment-{run_id}-normal",
        platform=Platform.INSTAGRAM,
        type=MessageType.COMMENT,
        sender_id="fake-user-1",
        sender_username="jane_doe",
        text="Do you guys build automations for CRMs?",
        received_at=now,
        detected_language=Language.ENGLISH,
        classification=Classification.QUESTION,
        status=MessageStatus.PENDING_APPROVAL,
    )
    insert_message(conn, message)

    draft = Draft(
        message_id=message.id,
        draft_text=(
            "Yes, we do! We build custom automations for CRMs, inboxes, and "
            "internal tools. Want to hop on a quick call to go over what you need?"
        ),
        created_at=now,
        draft_language="en",
        is_handoff=False,
    )
    draft_id = insert_draft(conn, draft)
    return message, draft_id


def seed_spam_item(conn, run_id: str) -> Message:
    now = dt.datetime.now().isoformat(timespec="seconds")
    message = Message(
        id=f"test-comment-{run_id}-spam",
        platform=Platform.INSTAGRAM,
        type=MessageType.COMMENT,
        sender_id="fake-user-2",
        sender_username="totally_legit_growth_hacker",
        text="DM us to collab and grow your followers fast!!",
        received_at=now,
        detected_language=Language.ENGLISH,
        classification=Classification.SPAM,
        status=MessageStatus.SPAM_PENDING,
    )
    insert_message(conn, message)
    return message


def main() -> None:
    config = load_config()
    init_db(config.db_path)

    send_callbacks = SendCallbacks(
        send_comment_reply=fake_send_comment_reply,
        send_dm_reply=fake_send_dm_reply,
        hide_comment=fake_hide_comment,
    )
    app = build_slack_app(bot_token=config.slack_bot_token, db_path=config.db_path, send_callbacks=send_callbacks)

    run_id = str(int(time.time()))
    conn = get_connection(config.db_path)
    try:
        message, draft_id = seed_normal_draft(conn, run_id)
        spam_message = seed_spam_item(conn, run_id)
    finally:
        conn.close()

    post_draft_for_approval(
        app,
        channel_id=config.slack_approvals_channel_id,
        db_path=config.db_path,
        message=message,
        draft_id=draft_id,
    )
    post_spam_check(
        app,
        channel_id=config.slack_approvals_channel_id,
        db_path=config.db_path,
        message=spam_message,
    )

    print("Posted a normal draft and a spam-check message to the approvals channel.")
    print("On the first: click Approve, Edit, or Reject.")
    print("On the second: click Hide or Keep.")
    print("Starting Socket Mode handler -- press Ctrl+C to stop.\n")

    handler = SocketModeHandler(app, config.slack_app_token)
    handler.start()


if __name__ == "__main__":
    main()
