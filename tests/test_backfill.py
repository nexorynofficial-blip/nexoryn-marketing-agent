import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app.claude_client import ClassificationResult, DraftResult
from app.db import get_connection, get_message, init_db
from app.knowledge import Knowledge
from app.models import Classification, Language, MessageStatus, MessageType, Platform
from app.pipeline import PipelineContext
from backfill import run_backfill

# run_backfill() computes its cutoff from the real dt.datetime.now(), so
# these fixture timestamps must be relative to it too, not a fixed date.
NOW = dt.datetime.now()
RECENT = (NOW - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+0000")
OLD = (NOW - dt.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S+0000")

OWN_IDS = {"PAGE123", "IG456"}


class FakeClaude:
    def classify_message(self, text):
        return ClassificationResult(category=Classification.QUESTION, language=Language.ENGLISH)

    def draft_reply(self, **kwargs):
        return DraftResult(text="Yes, we can help!", is_handoff=False)

    def translate_to_english(self, text):
        return text


class FakeMeta:
    def __init__(
        self,
        posts=None,
        media=None,
        post_comments=None,
        media_comments=None,
        conversations=None,
        conversation_messages=None,
        raise_on=None,
    ):
        self.posts = posts or []
        self.media = media or []
        self.post_comments = post_comments or {}
        self.media_comments = media_comments or {}
        self.conversations = conversations or []
        self.conversation_messages = conversation_messages or {}
        self.raise_on = raise_on or set()

    def get_recent_posts(self):
        return {"data": [{"id": p} for p in self.posts]}

    def get_recent_media(self):
        return {"data": [{"id": m} for m in self.media]}

    def get_comments_for_post(self, post_id):
        if post_id in self.raise_on:
            raise RuntimeError("Graph API down")
        return {"data": self.post_comments.get(post_id, [])}

    def get_comments_for_media(self, media_id):
        if media_id in self.raise_on:
            raise RuntimeError("Graph API down")
        return {"data": self.media_comments.get(media_id, [])}

    def get_conversations(self):
        return {"data": [{"id": c} for c in self.conversations]}

    def get_conversation(self, conversation_id, limit=20):
        if conversation_id in self.raise_on:
            raise RuntimeError("Graph API down")
        return {"data": self.conversation_messages.get(conversation_id, [])}


def make_knowledge() -> Knowledge:
    return Knowledge(raw_text="doc", handoff_messages={}, pricing_line="Pricing depends on your needs.")


def make_ctx(tmp_path, meta) -> PipelineContext:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return PipelineContext(
        db_path=db_path,
        slack_app=None,
        slack_channel_id="C1",
        claude=FakeClaude(),
        knowledge=make_knowledge(),
        meta=meta,
    )


def patch_slack_posting(monkeypatch):
    calls = []
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: calls.append(kw) or "ts")
    monkeypatch.setattr("app.pipeline.post_spam_check", lambda *a, **kw: calls.append(kw) or "ts")
    monkeypatch.setattr("app.pipeline.post_alert_only", lambda *a, **kw: calls.append(kw) or "ts")
    return calls


def test_no_meta_client_returns_zero(tmp_path, monkeypatch):
    patch_slack_posting(monkeypatch)
    ctx = make_ctx(tmp_path, meta=None)

    assert run_backfill(ctx, OWN_IDS) == 0


def test_recent_facebook_comment_is_processed(tmp_path, monkeypatch):
    calls = patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        posts=["post1"],
        post_comments={
            "post1": [
                {"id": "c1", "message": "Do you build sites?", "from": {"id": "u1", "name": "Jane"}, "created_time": RECENT}
            ]
        },
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS)

    assert count == 1
    assert len(calls) == 1
    conn = get_connection(ctx.db_path)
    message = get_message(conn, "c1")
    conn.close()
    assert message is not None
    assert message.platform == Platform.FACEBOOK
    assert message.type == MessageType.COMMENT


def test_recent_instagram_media_comment_is_processed(tmp_path, monkeypatch):
    calls = patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        media=["media1"],
        media_comments={
            "media1": [{"id": "c2", "text": "Nice work", "username": "jane", "from": {"id": "u2"}, "timestamp": RECENT}]
        },
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS)

    assert count == 1
    conn = get_connection(ctx.db_path)
    message = get_message(conn, "c2")
    conn.close()
    assert message.platform == Platform.INSTAGRAM


def test_own_comment_is_skipped(tmp_path, monkeypatch):
    calls = patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        posts=["post1"],
        post_comments={"post1": [{"id": "c1", "message": "our reply", "from": {"id": "PAGE123"}, "created_time": RECENT}]},
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS)

    assert count == 0
    assert len(calls) == 0


def test_comment_older_than_window_is_skipped(tmp_path, monkeypatch):
    calls = patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        posts=["post1"],
        post_comments={"post1": [{"id": "c1", "message": "old", "from": {"id": "u1"}, "created_time": OLD}]},
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS, hours=24)

    assert count == 0
    assert len(calls) == 0


def test_already_processed_comment_is_skipped(tmp_path, monkeypatch):
    from app.db import insert_message
    from app.models import Message

    patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        posts=["post1"],
        post_comments={"post1": [{"id": "c1", "message": "hi", "from": {"id": "u1"}, "created_time": RECENT}]},
    )
    ctx = make_ctx(tmp_path, meta)

    conn = get_connection(ctx.db_path)
    insert_message(
        conn,
        Message(
            id="c1", platform=Platform.FACEBOOK, type=MessageType.COMMENT, sender_id="u1",
            text="hi", received_at="2026-09-26T10:00:00", status=MessageStatus.SENT,
        ),
    )
    conn.close()

    count = run_backfill(ctx, OWN_IDS)

    assert count == 0


def test_comment_missing_sender_id_is_skipped(tmp_path, monkeypatch):
    patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        media=["media1"],
        media_comments={"media1": [{"id": "c1", "text": "hi", "timestamp": RECENT}]},  # no "from" at all
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS)

    assert count == 0


def test_one_failing_container_does_not_stop_the_rest(tmp_path, monkeypatch):
    patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        posts=["bad_post", "good_post"],
        post_comments={"good_post": [{"id": "c1", "message": "hi", "from": {"id": "u1"}, "created_time": RECENT}]},
        raise_on={"bad_post"},
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS)

    assert count == 1


def test_dm_with_recent_customer_message_is_processed(tmp_path, monkeypatch):
    calls = patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        conversations=["conv1"],
        conversation_messages={
            "conv1": [{"id": "m1", "message": "How much for automation?", "from": {"id": "u1"}, "created_time": RECENT}]
        },
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS)

    assert count == 1
    assert len(calls) == 1
    conn = get_connection(ctx.db_path)
    message = get_message(conn, "m1")
    conn.close()
    assert message.type == MessageType.DM


def test_dm_already_answered_by_us_is_skipped(tmp_path, monkeypatch):
    calls = patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        conversations=["conv1"],
        conversation_messages={
            "conv1": [{"id": "m1", "message": "our reply", "from": {"id": "PAGE123"}, "created_time": RECENT}]
        },
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS)

    assert count == 0
    assert len(calls) == 0


def test_dm_outside_window_is_skipped(tmp_path, monkeypatch):
    patch_slack_posting(monkeypatch)
    meta = FakeMeta(
        conversations=["conv1"],
        conversation_messages={"conv1": [{"id": "m1", "message": "old", "from": {"id": "u1"}, "created_time": OLD}]},
    )
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS, hours=24)

    assert count == 0


def test_limit_caps_total_processed(tmp_path, monkeypatch):
    patch_slack_posting(monkeypatch)
    comments = [
        {"id": f"c{i}", "message": "hi", "from": {"id": f"u{i}"}, "created_time": RECENT} for i in range(5)
    ]
    meta = FakeMeta(posts=["post1"], post_comments={"post1": comments})
    ctx = make_ctx(tmp_path, meta)

    count = run_backfill(ctx, OWN_IDS, limit=2)

    assert count == 2
