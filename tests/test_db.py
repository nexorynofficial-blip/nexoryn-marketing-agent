from pathlib import Path

from app.db import (
    get_connection,
    get_draft,
    get_draft_for_message,
    get_message,
    get_pending_dm_message_by_sender,
    init_db,
    insert_draft,
    insert_message,
    list_expiring_unnudged_dm_drafts,
    list_pending_comment_message_ids,
    update_draft_approval,
    update_draft_edited_text,
    update_draft_nudged,
    update_draft_sent,
    update_draft_slack_ts,
    update_message_status,
)
from app.models import Classification, Draft, Language, Message, MessageStatus, MessageType, Platform


def make_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


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


def test_insert_and_get_message_round_trips_enums(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message())
        loaded = get_message(conn, "msg-1")

        assert loaded.id == "msg-1"
        assert loaded.platform == Platform.INSTAGRAM
        assert loaded.type == MessageType.COMMENT
        assert loaded.detected_language == Language.ENGLISH
        assert loaded.classification == Classification.QUESTION
        assert loaded.status == MessageStatus.PENDING_APPROVAL
    finally:
        conn.close()


def test_get_message_missing_returns_none(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        assert get_message(conn, "does-not-exist") is None
    finally:
        conn.close()


def test_update_message_status(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message())
        update_message_status(conn, "msg-1", MessageStatus.SENT)
        loaded = get_message(conn, "msg-1")
        assert loaded.status == MessageStatus.SENT
    finally:
        conn.close()


def test_insert_and_get_draft_round_trip(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message())
        draft = Draft(
            message_id="msg-1",
            draft_text="Yes, we build custom websites!",
            created_at="2026-09-26T10:01:00",
            draft_language="en",
            is_handoff=False,
        )
        draft_id = insert_draft(conn, draft)
        loaded = get_draft(conn, draft_id)

        assert loaded.message_id == "msg-1"
        assert loaded.draft_text == "Yes, we build custom websites!"
        assert loaded.is_handoff is False
        assert loaded.edited_text is None
    finally:
        conn.close()


def test_draft_approval_sent_and_edited_text_updates(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message())
        draft_id = insert_draft(
            conn,
            Draft(message_id="msg-1", draft_text="Original AI draft", created_at="2026-09-26T10:01:00"),
        )

        update_draft_slack_ts(conn, draft_id, "1234.5678")
        update_draft_approval(conn, draft_id, approved_by="U123", approved_at="2026-09-26T10:05:00")
        update_draft_sent(conn, draft_id, sent_at="2026-09-26T10:05:01")
        update_draft_edited_text(conn, draft_id, "Human-edited final reply")

        loaded = get_draft(conn, draft_id)
        assert loaded.slack_message_ts == "1234.5678"
        assert loaded.approved_by == "U123"
        assert loaded.approved_at == "2026-09-26T10:05:00"
        assert loaded.sent_at == "2026-09-26T10:05:01"
        assert loaded.edited_text == "Human-edited final reply"
        # draft_text (the original AI draft) must stay untouched -- both
        # versions need to remain recoverable from the DB.
        assert loaded.draft_text == "Original AI draft"
    finally:
        conn.close()


def test_get_pending_dm_message_by_sender_finds_most_recent(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message(
            id="dm-1", type=MessageType.DM, sender_id="cust-1",
            received_at="2026-09-26T09:00:00", status=MessageStatus.PENDING_APPROVAL,
        ))
        insert_message(conn, make_message(
            id="dm-2", type=MessageType.DM, sender_id="cust-1",
            received_at="2026-09-26T10:00:00", status=MessageStatus.PENDING_APPROVAL,
        ))
        # A different sender's pending DM must not be returned.
        insert_message(conn, make_message(
            id="dm-3", type=MessageType.DM, sender_id="cust-2",
            received_at="2026-09-26T11:00:00", status=MessageStatus.PENDING_APPROVAL,
        ))

        found = get_pending_dm_message_by_sender(conn, "cust-1")
        assert found.id == "dm-2"  # most recent
    finally:
        conn.close()


def test_get_pending_dm_message_by_sender_ignores_non_pending(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message(
            id="dm-1", type=MessageType.DM, sender_id="cust-1", status=MessageStatus.SENT,
        ))
        assert get_pending_dm_message_by_sender(conn, "cust-1") is None
    finally:
        conn.close()


def test_get_pending_dm_message_by_sender_ignores_comments(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message(
            id="c-1", type=MessageType.COMMENT, sender_id="cust-1", status=MessageStatus.PENDING_APPROVAL,
        ))
        assert get_pending_dm_message_by_sender(conn, "cust-1") is None
    finally:
        conn.close()


def test_get_draft_for_message(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message())
        draft_id = insert_draft(
            conn, Draft(message_id="msg-1", draft_text="Hi", created_at="2026-09-26T10:01:00")
        )
        found = get_draft_for_message(conn, "msg-1")
        assert found.id == draft_id
        assert get_draft_for_message(conn, "no-such-message") is None
    finally:
        conn.close()


def test_list_pending_comment_message_ids(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message(id="c-1", status=MessageStatus.PENDING_APPROVAL))
        insert_message(conn, make_message(id="c-2", status=MessageStatus.SENT))
        insert_message(conn, make_message(
            id="dm-1", type=MessageType.DM, status=MessageStatus.PENDING_APPROVAL,
        ))

        ids = list_pending_comment_message_ids(conn)
        assert ids == ["c-1"]
    finally:
        conn.close()


def test_list_expiring_unnudged_dm_drafts(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message(id="dm-1", type=MessageType.DM, status=MessageStatus.PENDING_APPROVAL))
        insert_message(conn, make_message(id="dm-2", type=MessageType.DM, status=MessageStatus.PENDING_APPROVAL))
        insert_message(conn, make_message(id="dm-3", type=MessageType.DM, status=MessageStatus.PENDING_APPROVAL))

        # Expiring soon, not yet nudged -- should be included.
        insert_draft(conn, Draft(
            message_id="dm-1", draft_text="x", created_at="2026-09-26T10:00:00",
            draft_expires_at="2026-09-26T12:00:00",
        ))
        # Expiring soon, but already nudged -- should be excluded.
        insert_draft(conn, Draft(
            message_id="dm-2", draft_text="x", created_at="2026-09-26T10:00:00",
            draft_expires_at="2026-09-26T12:00:00", nudged_at="2026-09-26T11:00:00",
        ))
        # Not expiring soon -- should be excluded.
        insert_draft(conn, Draft(
            message_id="dm-3", draft_text="x", created_at="2026-09-26T10:00:00",
            draft_expires_at="2026-09-27T12:00:00",
        ))

        results = list_expiring_unnudged_dm_drafts(conn, cutoff_iso="2026-09-26T14:00:00")

        assert len(results) == 1
        message, draft = results[0]
        assert message.id == "dm-1"
        assert draft.draft_expires_at == "2026-09-26T12:00:00"
    finally:
        conn.close()


def test_update_draft_nudged(tmp_path):
    db_path = make_db(tmp_path)
    conn = get_connection(db_path)
    try:
        insert_message(conn, make_message())
        draft_id = insert_draft(conn, Draft(message_id="msg-1", draft_text="x", created_at="2026-09-26T10:00:00"))

        update_draft_nudged(conn, draft_id, "2026-09-26T11:00:00")

        loaded = get_draft(conn, draft_id)
        assert loaded.nudged_at == "2026-09-26T11:00:00"
    finally:
        conn.close()
