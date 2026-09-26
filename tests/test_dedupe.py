from app.db import get_connection, init_db
from app.dedupe import is_duplicate_webhook_event, mark_webhook_event_processed


def make_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


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
