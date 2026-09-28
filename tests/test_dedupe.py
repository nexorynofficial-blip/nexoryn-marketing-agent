from unittest.mock import MagicMock

from app.db import get_connection, get_draft, get_message, init_db, insert_draft, insert_message
from app.dedupe import (
    handle_comment_echo,
    handle_dm_echo,
    is_duplicate_webhook_event,
    mark_webhook_event_processed,
    run_comment_echo_poll,
    run_expiry_nudge_check,
)
from app.knowledge import Knowledge
from app.models import Draft, Language, Message, MessageStatus, MessageType, Platform
from app.pipeline import PipelineContext


def make_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


def make_message(**overrides) -> Message:
    defaults = dict(
        id="m1",
        platform=Platform.INSTAGRAM,
        type=MessageType.DM,
        sender_id="cust-1",
        text="hi",
        received_at="2026-09-26T10:00:00",
        status=MessageStatus.PENDING_APPROVAL,
    )
    defaults.update(overrides)
    return Message(**defaults)


def make_knowledge() -> Knowledge:
    return Knowledge(raw_text="doc", handoff_messages={}, pricing_line="Pricing depends on your needs.")


def make_ctx(tmp_path, meta=None):
    db_path = make_db(tmp_path)
    fake_slack = MagicMock()
    ctx = PipelineContext(
        db_path=db_path,
        slack_app=fake_slack,
        slack_channel_id="C1",
        claude=None,
        knowledge=make_knowledge(),
        meta=meta,
    )
    return ctx, fake_slack


def test_new_event_is_not_duplicate(tmp_path):
    conn = get_connection(make_db(tmp_path))
    try:
        assert is_duplicate_webhook_event(conn, "comment:123") is False
    finally:
        conn.close()


def test_marked_event_is_duplicate_on_next_check(tmp_path):
    conn = get_connection(make_db(tmp_path))
    try:
        mark_webhook_event_processed(conn, "comment:123")
        assert is_duplicate_webhook_event(conn, "comment:123") is True
        assert is_duplicate_webhook_event(conn, "comment:456") is False
    finally:
        conn.close()


def test_marking_the_same_event_twice_does_not_raise(tmp_path):
    conn = get_connection(make_db(tmp_path))
    try:
        mark_webhook_event_processed(conn, "message:abc")
        mark_webhook_event_processed(conn, "message:abc")  # simulates a Meta retry
        assert is_duplicate_webhook_event(conn, "message:abc") is True
    finally:
        conn.close()


class TestHandleDmEcho:
    def test_cancels_pending_dm_draft(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="dm-1", sender_id="cust-1"))
        draft_id = insert_draft(
            conn, Draft(message_id="dm-1", draft_text="Hi!", created_at="2026-09-26T10:00:00", slack_message_ts="111.222")
        )
        conn.close()

        handle_dm_echo(ctx, recipient_id="cust-1")

        conn = get_connection(ctx.db_path)
        message = get_message(conn, "dm-1")
        conn.close()
        assert message.status == MessageStatus.ALREADY_HANDLED
        fake_slack.client.chat_update.assert_called_once()
        call_kwargs = fake_slack.client.chat_update.call_args.kwargs
        assert call_kwargs["ts"] == "111.222"

    def test_no_pending_dm_is_a_noop(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)

        handle_dm_echo(ctx, recipient_id="unknown-customer")

        fake_slack.client.chat_update.assert_not_called()

    def test_does_not_affect_a_different_senders_pending_draft(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="dm-1", sender_id="cust-1"))
        insert_draft(conn, Draft(message_id="dm-1", draft_text="Hi!", created_at="2026-09-26T10:00:00"))
        conn.close()

        handle_dm_echo(ctx, recipient_id="cust-2")

        conn = get_connection(ctx.db_path)
        message = get_message(conn, "dm-1")
        conn.close()
        assert message.status == MessageStatus.PENDING_APPROVAL
        fake_slack.client.chat_update.assert_not_called()


class TestHandleCommentEcho:
    def test_cancels_pending_comment_draft(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="c1", type=MessageType.COMMENT, sender_id="cust-1"))
        insert_draft(
            conn, Draft(message_id="c1", draft_text="Thanks!", created_at="2026-09-26T10:00:00", slack_message_ts="333.444")
        )
        conn.close()

        handle_comment_echo(ctx, parent_comment_id="c1")

        conn = get_connection(ctx.db_path)
        message = get_message(conn, "c1")
        conn.close()
        assert message.status == MessageStatus.ALREADY_HANDLED
        fake_slack.client.chat_update.assert_called_once()

    def test_unknown_comment_is_a_noop(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)
        handle_comment_echo(ctx, parent_comment_id="does-not-exist")
        fake_slack.client.chat_update.assert_not_called()

    def test_already_resolved_comment_is_a_noop(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="c1", type=MessageType.COMMENT, status=MessageStatus.SENT))
        conn.close()

        handle_comment_echo(ctx, parent_comment_id="c1")

        fake_slack.client.chat_update.assert_not_called()


class FakeMeta:
    def __init__(self, replies_by_comment=None, raise_for=None):
        self.replies_by_comment = replies_by_comment or {}
        self.raise_for = raise_for or set()
        self.calls = []

    def get_comment_replies(self, comment_id):
        self.calls.append(comment_id)
        if comment_id in self.raise_for:
            raise RuntimeError("Graph API is down")
        return {"data": self.replies_by_comment.get(comment_id, [])}


class TestRunCommentEchoPoll:
    def test_cancels_comment_with_reply_from_own_account(self, tmp_path):
        meta = FakeMeta(replies_by_comment={"c1": [{"from": {"id": "PAGE123"}}]})
        ctx, fake_slack = make_ctx(tmp_path, meta=meta)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="c1", type=MessageType.COMMENT))
        conn.close()

        cancelled = run_comment_echo_poll(ctx, own_ids={"PAGE123", "IG456"})

        assert cancelled == 1
        conn = get_connection(ctx.db_path)
        message = get_message(conn, "c1")
        conn.close()
        assert message.status == MessageStatus.ALREADY_HANDLED

    def test_no_reply_leaves_draft_pending(self, tmp_path):
        meta = FakeMeta(replies_by_comment={"c1": [{"from": {"id": "some_other_user"}}]})
        ctx, fake_slack = make_ctx(tmp_path, meta=meta)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="c1", type=MessageType.COMMENT))
        conn.close()

        cancelled = run_comment_echo_poll(ctx, own_ids={"PAGE123", "IG456"})

        assert cancelled == 0

    def test_one_failing_lookup_does_not_stop_the_sweep(self, tmp_path):
        meta = FakeMeta(
            replies_by_comment={"c2": [{"from": {"id": "PAGE123"}}]},
            raise_for={"c1"},
        )
        ctx, fake_slack = make_ctx(tmp_path, meta=meta)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="c1", type=MessageType.COMMENT))
        insert_message(conn, make_message(id="c2", type=MessageType.COMMENT))
        conn.close()

        cancelled = run_comment_echo_poll(ctx, own_ids={"PAGE123"})

        assert cancelled == 1  # c1 failed and was skipped, c2 still processed
        assert set(meta.calls) == {"c1", "c2"}

    def test_no_meta_client_returns_zero(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path, meta=None)
        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="c1", type=MessageType.COMMENT))
        conn.close()

        assert run_comment_echo_poll(ctx, own_ids={"PAGE123"}) == 0


class TestRunExpiryNudgeCheck:
    def test_nudges_expiring_unnudged_dm_draft(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)
        fake_slack.client.chat_postMessage.return_value = {"ts": "555.666"}

        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="dm-1", type=MessageType.DM))
        draft_id = insert_draft(
            conn,
            Draft(
                message_id="dm-1",
                draft_text="Yes!",
                created_at="2026-09-26T10:00:00",
                draft_expires_at="2026-09-26T12:00:00",
            ),
        )
        conn.close()

        nudged = run_expiry_nudge_check(ctx, within_hours=1000)  # generous window so it always matches "now"

        assert nudged == 1
        fake_slack.client.chat_postMessage.assert_called_once()
        conn = get_connection(ctx.db_path)
        draft = get_draft(conn, draft_id)
        conn.close()
        assert draft.nudged_at is not None

    def test_does_not_nudge_twice(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)
        fake_slack.client.chat_postMessage.return_value = {"ts": "1.1"}

        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="dm-1", type=MessageType.DM))
        insert_draft(
            conn,
            Draft(
                message_id="dm-1",
                draft_text="Yes!",
                created_at="2026-09-26T10:00:00",
                draft_expires_at="2026-09-26T12:00:00",
            ),
        )
        conn.close()

        first = run_expiry_nudge_check(ctx, within_hours=1000)
        second = run_expiry_nudge_check(ctx, within_hours=1000)

        assert first == 1
        assert second == 0  # already nudged, skipped this time
        assert fake_slack.client.chat_postMessage.call_count == 1

    def test_does_not_nudge_drafts_not_yet_close_to_expiry(self, tmp_path):
        ctx, fake_slack = make_ctx(tmp_path)

        conn = get_connection(ctx.db_path)
        insert_message(conn, make_message(id="dm-1", type=MessageType.DM))
        insert_draft(
            conn,
            Draft(
                message_id="dm-1",
                draft_text="Yes!",
                created_at="2026-09-26T10:00:00",
                draft_expires_at="2099-01-01T00:00:00",  # far in the future
            ),
        )
        conn.close()

        nudged = run_expiry_nudge_check(ctx, within_hours=4)

        assert nudged == 0
        fake_slack.client.chat_postMessage.assert_not_called()
