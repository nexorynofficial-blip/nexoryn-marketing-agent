from unittest.mock import MagicMock

from app.db import get_connection, init_db, insert_draft, insert_message
from app.models import Classification, Draft, Language, Message, MessageStatus, MessageType, Platform
from app.slack_app import (
    _apply_edit_to_blocks,
    _build_already_handled_blocks,
    _build_pending_blocks,
    _build_spam_blocks,
    _label,
    _replace_actions_with_resolution,
    post_expiry_nudge,
    update_message_as_already_handled,
)


def make_message(**overrides) -> Message:
    defaults = dict(
        id="msg-1",
        platform=Platform.INSTAGRAM,
        type=MessageType.COMMENT,
        sender_id="user-1",
        sender_username="jane_doe",
        text="Do you build websites?",
        received_at="2026-09-26T10:00:00",
        detected_language=Language.ENGLISH,
        classification=Classification.QUESTION,
        status=MessageStatus.PENDING_APPROVAL,
    )
    defaults.update(overrides)
    return Message(**defaults)


def make_draft(**overrides) -> Draft:
    defaults = dict(
        id=1,
        message_id="msg-1",
        draft_text="Yes, we build custom websites!",
        created_at="2026-09-26T10:01:00",
        draft_language="en",
        is_handoff=False,
    )
    defaults.update(overrides)
    return Draft(**defaults)


def find_block(blocks, block_id):
    return next((b for b in blocks if b.get("block_id") == block_id), None)


def find_actions_block(blocks):
    return next((b for b in blocks if b.get("type") == "actions"), None)


class TestLabel:
    def test_instagram_comment(self):
        assert _label(make_message(platform=Platform.INSTAGRAM, type=MessageType.COMMENT)) == "📷 Instagram Comment"

    def test_facebook_dm(self):
        assert _label(make_message(platform=Platform.FACEBOOK, type=MessageType.DM)) == "📘 Facebook DM"


class TestBuildPendingBlocks:
    def test_has_approve_edit_reject_buttons(self):
        blocks = _build_pending_blocks(make_message(), make_draft())
        actions = find_actions_block(blocks)

        action_ids = [el["action_id"] for el in actions["elements"]]
        assert action_ids == ["approve_draft", "edit_draft", "reject_draft"]

    def test_button_value_carries_message_and_draft_id(self):
        import json

        blocks = _build_pending_blocks(make_message(id="msg-42"), make_draft(id=7))
        actions = find_actions_block(blocks)
        value = json.loads(actions["elements"][0]["value"])

        assert value == {"message_id": "msg-42", "draft_id": 7}

    def test_omits_translation_blocks_when_not_provided(self):
        blocks = _build_pending_blocks(make_message(), make_draft())

        assert find_block(blocks, "message_translation_block") is None
        assert find_block(blocks, "draft_translation_block") is None

    def test_includes_translation_blocks_when_provided(self):
        blocks = _build_pending_blocks(
            make_message(),
            make_draft(),
            message_translation="Do you build websites? (EN)",
            draft_translation="Yes we do (EN)",
        )

        assert find_block(blocks, "message_translation_block") is not None
        assert find_block(blocks, "draft_translation_block") is not None

    def test_handoff_draft_labeled_as_handoff(self):
        blocks = _build_pending_blocks(make_message(), make_draft(is_handoff=True))
        draft_block = find_block(blocks, "draft_text_block")

        assert "handoff" in draft_block["text"]["text"].lower()

    def test_edited_text_shown_over_original_draft(self):
        blocks = _build_pending_blocks(make_message(), make_draft(edited_text="Edited version"))
        draft_block = find_block(blocks, "draft_text_block")

        assert "Edited version" in draft_block["text"]["text"]
        assert "Yes, we build custom websites!" not in draft_block["text"]["text"]


class TestBuildSpamBlocks:
    def test_has_hide_and_keep_buttons_only(self):
        blocks = _build_spam_blocks(make_message(classification=Classification.SPAM))
        actions = find_actions_block(blocks)

        action_ids = [el["action_id"] for el in actions["elements"]]
        assert action_ids == ["hide_comment", "keep_comment"]

    def test_no_draft_text_block(self):
        blocks = _build_spam_blocks(make_message(classification=Classification.SPAM))
        assert find_block(blocks, "draft_text_block") is None


class TestReplaceActionsWithResolution:
    def test_removes_actions_and_appends_context(self):
        blocks = _build_pending_blocks(make_message(), make_draft())

        resolved = _replace_actions_with_resolution(blocks, "✅ Approved by <@U123> at 3:42 PM")

        assert find_actions_block(resolved) is None
        assert resolved[-1]["type"] == "context"
        assert "Approved by <@U123>" in resolved[-1]["elements"][0]["text"]


class TestApplyEditToBlocks:
    def test_replaces_draft_text_and_drops_stale_translation(self):
        blocks = _build_pending_blocks(
            make_message(),
            make_draft(),
            draft_translation="stale translation of the old draft",
        )

        edited = _apply_edit_to_blocks(blocks, "This is what the human actually wants to send")

        draft_block = find_block(edited, "draft_text_block")
        assert "This is what the human actually wants to send" in draft_block["text"]["text"]
        assert find_block(edited, "draft_translation_block") is None

    def test_leaves_other_blocks_untouched(self):
        blocks = _build_pending_blocks(
            make_message(),
            make_draft(),
            message_translation="kept because it's about the original message",
        )

        edited = _apply_edit_to_blocks(blocks, "New text")

        assert find_block(edited, "message_translation_block") is not None
        assert find_block(edited, "message_text_block") is not None


class TestBuildAlreadyHandledBlocks:
    def test_no_action_buttons(self):
        blocks = _build_already_handled_blocks(make_message(), "A teammate already replied.")
        assert find_actions_block(blocks) is None

    def test_includes_reason(self):
        blocks = _build_already_handled_blocks(make_message(), "A teammate already replied via Business Suite.")
        assert any("teammate already replied" in str(b) for b in blocks)


class TestUpdateMessageAsAlreadyHandled:
    def test_calls_chat_update_with_no_buttons(self):
        fake_app = MagicMock()
        update_message_as_already_handled(
            fake_app, channel_id="C1", ts="123.456", message=make_message(), reason="A teammate replied first."
        )

        fake_app.client.chat_update.assert_called_once()
        call_kwargs = fake_app.client.chat_update.call_args.kwargs
        assert call_kwargs["channel"] == "C1"
        assert call_kwargs["ts"] == "123.456"
        assert find_actions_block(call_kwargs["blocks"]) is None


class TestPostExpiryNudge:
    def make_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        return db_path

    def test_posts_with_same_action_ids_and_updates_slack_ts(self, tmp_path):
        import json

        db_path = self.make_db(tmp_path)
        conn = get_connection(db_path)
        message = make_message(id="dm-1", type=MessageType.DM)
        insert_message(conn, message)
        draft_id = insert_draft(
            conn, Draft(message_id="dm-1", draft_text="Yes!", created_at="2026-09-26T10:00:00")
        )
        conn.close()

        fake_app = MagicMock()
        fake_app.client.chat_postMessage.return_value = {"ts": "999.111"}

        ts = post_expiry_nudge(
            fake_app,
            channel_id="C1",
            db_path=db_path,
            message=message,
            draft_id=draft_id,
            expires_at_display="4:00 PM",
        )

        assert ts == "999.111"
        call_kwargs = fake_app.client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]
        actions = find_actions_block(blocks)
        action_ids = [el["action_id"] for el in actions["elements"]]
        assert action_ids == ["approve_draft", "edit_draft", "reject_draft"]

        value = json.loads(actions["elements"][0]["value"])
        assert value == {"message_id": "dm-1", "draft_id": draft_id}

        conn = get_connection(db_path)
        from app.db import get_draft

        updated = get_draft(conn, draft_id)
        conn.close()
        assert updated.slack_message_ts == "999.111"

    def test_includes_expiry_warning_text(self, tmp_path):
        db_path = self.make_db(tmp_path)
        conn = get_connection(db_path)
        message = make_message(id="dm-2", type=MessageType.DM)
        insert_message(conn, message)
        draft_id = insert_draft(
            conn, Draft(message_id="dm-2", draft_text="Yes!", created_at="2026-09-26T10:00:00")
        )
        conn.close()

        fake_app = MagicMock()
        fake_app.client.chat_postMessage.return_value = {"ts": "1.1"}

        post_expiry_nudge(
            fake_app, channel_id="C1", db_path=db_path, message=message, draft_id=draft_id,
            expires_at_display="4:00 PM",
        )

        blocks = fake_app.client.chat_postMessage.call_args.kwargs["blocks"]
        assert any("Expiring soon" in str(b) and "4:00 PM" in str(b) for b in blocks)
