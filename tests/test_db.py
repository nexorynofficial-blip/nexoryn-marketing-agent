from app.db import (
    get_comments_for_post,
    get_connection,
    get_last_summary,
    get_post,
    get_posts_since,
    get_recent_summaries,
    get_unanswered_comments,
    init_db,
    store_comments,
    store_messages,
    store_posts,
    store_summary,
    update_comment_sentiment,
    update_message_theme,
)
from app.models import Comment, Message, Post, Summary


def make_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


def make_post(**overrides) -> Post:
    defaults = dict(
        id="post-1",
        message="Check out our new product!",
        created_time="2026-10-01T10:00:00",
        reactions_count=10,
        comments_count=2,
        engagement_score=12,
        fetched_at="2026-10-01T12:00:00",
    )
    defaults.update(overrides)
    return Post(**defaults)


def make_comment(**overrides) -> Comment:
    defaults = dict(
        id="comment-1",
        post_id="post-1",
        text="Is this available in Europe?",
        author_name="Sarah M.",
        created_time="2026-10-01T10:15:00",
        has_page_reply=False,
        fetched_at="2026-10-01T12:00:00",
    )
    defaults.update(overrides)
    return Comment(**defaults)


def make_message(**overrides) -> Message:
    defaults = dict(
        id="msg-1",
        conversation_id="conv-1",
        sender_id="user-1",
        text="When will my order ship?",
        created_time="2026-10-01T11:00:00",
        is_from_page=False,
        fetched_at="2026-10-01T12:00:00",
    )
    defaults.update(overrides)
    return Message(**defaults)


class TestPosts:
    def test_store_and_get_post(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            store_posts(conn, [make_post()], now="2026-10-01T12:00:00")
            loaded = get_post(conn, "post-1")
            assert loaded.message == "Check out our new product!"
            assert loaded.engagement_score == 12
        finally:
            conn.close()

    def test_upsert_preserves_created_at_but_updates_fields(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            store_posts(conn, [make_post()], now="2026-10-01T12:00:00")
            created_at_before = conn.execute("SELECT created_at FROM posts WHERE id = 'post-1'").fetchone()[0]

            store_posts(conn, [make_post(reactions_count=50, engagement_score=52)], now="2026-10-01T14:00:00")

            row = conn.execute("SELECT created_at, updated_at, reactions_count FROM posts WHERE id = 'post-1'").fetchone()
            assert row["created_at"] == created_at_before  # unchanged
            assert row["updated_at"] == "2026-10-01T14:00:00"  # bumped
            assert row["reactions_count"] == 50
        finally:
            conn.close()

    def test_get_posts_since_filters_by_cutoff(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            store_posts(
                conn,
                [
                    make_post(id="old", created_time="2026-09-29T00:00:00"),
                    make_post(id="recent", created_time="2026-10-01T11:00:00"),
                ],
                now="2026-10-01T12:00:00",
            )
            results = get_posts_since(conn, cutoff_iso="2026-10-01T00:00:00")
            assert [p.id for p in results] == ["recent"]
        finally:
            conn.close()


class TestComments:
    def test_store_and_get_comments_for_post(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            store_posts(conn, [make_post()], now="2026-10-01T12:00:00")
            store_comments(conn, [make_comment()], now="2026-10-01T12:00:00")
            results = get_comments_for_post(conn, "post-1")
            assert len(results) == 1
            assert results[0].author_name == "Sarah M."
            assert results[0].has_page_reply is False
        finally:
            conn.close()

    def test_get_unanswered_comments_excludes_answered(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            store_posts(conn, [make_post()], now="2026-10-01T12:00:00")
            store_comments(
                conn,
                [
                    make_comment(id="c1", has_page_reply=False, created_time="2026-10-01T10:00:00"),
                    make_comment(id="c2", has_page_reply=True, created_time="2026-10-01T10:05:00"),
                ],
                now="2026-10-01T12:00:00",
            )
            results = get_unanswered_comments(conn, cutoff_iso="2026-10-01T00:00:00")
            assert [c.id for c in results] == ["c1"]
        finally:
            conn.close()

    def test_sentiment_preserved_on_upsert_unless_explicitly_set(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            store_posts(conn, [make_post()], now="2026-10-01T12:00:00")
            store_comments(conn, [make_comment()], now="2026-10-01T12:00:00")
            update_comment_sentiment(conn, "comment-1", "positive")

            # Re-fetching the same comment (sentiment=None on the fresh object)
            # must not wipe out the sentiment we already computed.
            store_comments(conn, [make_comment(has_page_reply=True)], now="2026-10-01T14:00:00")

            result = get_comments_for_post(conn, "post-1")[0]
            assert result.sentiment == "positive"
            assert result.has_page_reply is True
        finally:
            conn.close()


class TestMessages:
    def test_store_and_theme_persistence(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            store_messages(conn, [make_message()], now="2026-10-01T12:00:00")
            update_message_theme(conn, "msg-1", "Order status")

            # A re-fetch with theme=None must not clobber the saved theme.
            store_messages(conn, [make_message(text="updated text")], now="2026-10-01T14:00:00")

            from app.db import get_messages_since

            result = get_messages_since(conn, cutoff_iso="2026-10-01T00:00:00")[0]
            assert result.theme == "Order status"
            assert result.text == "updated text"
        finally:
            conn.close()


class TestSummaries:
    def test_store_and_get_last_summary(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            s = Summary(timestamp="2026-10-01T17:45:00", status="healthy", message="all good")
            store_summary(conn, s, now="2026-10-01T17:45:00")

            loaded = get_last_summary(conn)
            assert loaded.status == "healthy"
            assert loaded.message == "all good"
        finally:
            conn.close()

    def test_get_last_summary_none_when_empty(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            assert get_last_summary(conn) is None
        finally:
            conn.close()

    def test_get_recent_summaries_newest_first_and_limited(self, tmp_path):
        conn = get_connection(make_db(tmp_path))
        try:
            for i in range(7):
                store_summary(
                    conn,
                    Summary(timestamp=f"2026-10-01T1{i}:00:00", status="healthy", message="ok"),
                    now=f"2026-10-01T1{i}:00:00",
                )
            recent = get_recent_summaries(conn, limit=5)
            assert len(recent) == 5
            assert recent[0].timestamp == "2026-10-01T16:00:00"  # most recent first
        finally:
            conn.close()
