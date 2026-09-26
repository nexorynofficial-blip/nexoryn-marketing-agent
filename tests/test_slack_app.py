from app.models import Classification, Draft, Language, Message, MessageStatus, MessageType, Platform
from app.slack_app import (
    _apply_edit_to_blocks,
    _build_pending_blocks,
    _build_spam_blocks,
    _label,
    _replace_actions_with_resolution,
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
