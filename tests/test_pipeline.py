from app.claude_client import ClassificationResult, DraftResult
from app.db import get_connection, get_draft, get_message, init_db, insert_message
from app.knowledge import Knowledge
from app.models import Classification, Language, Message, MessageStatus, MessageType, Platform
from app.pipeline import CommentEvent, MessageEvent, PipelineContext, process_comment_event, process_message_event


class FakeClaude:
    def __init__(self, classification: ClassificationResult, draft: DraftResult, translation="EN translation"):
        self.classification = classification
        self.draft = draft
        self.translation = translation
        self.draft_calls = []
        self.classify_calls = []

    def classify_message(self, text):
        self.classify_calls.append(text)
        return self.classification

    def draft_reply(self, *, message_text, classification, language, is_handoff, conversation_history=None):
        self.draft_calls.append({"is_handoff": is_handoff, "classification": classification, "language": language})
        return self.draft

    def translate_to_english(self, text):
        return self.translation


def make_knowledge(pricing_line="Pricing depends on your needs, so let's talk on a call."):
    return Knowledge(
        raw_text="doc",
        handoff_messages={
            Language.ENGLISH: "Thanks! - The Nexoryn Team",
            Language.URDU_SCRIPT: "شکریہ - ٹیم",
            Language.URDU_ROMAN: "Shukriya - The Nexoryn Team",
        },
        pricing_line=pricing_line,
    )


def make_ctx(tmp_path, claude, knowledge=None) -> PipelineContext:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return PipelineContext(
        db_path=db_path,
        slack_app=None,
        slack_channel_id="C123",
        claude=claude,
        knowledge=knowledge or make_knowledge(),
        meta=None,
    )


def comment_event(comment_id="c1", text="hi", sender_id="u1", sender_username="jane") -> CommentEvent:
    return CommentEvent(
        dedup_key=f"comment:{comment_id}",
        comment_id=comment_id,
        platform=Platform.INSTAGRAM,
        sender_id=sender_id,
        sender_username=sender_username,
        text=text,
    )


def dm_event(message_id="m1", text="hi", sender_id="u1") -> MessageEvent:
    return MessageEvent(
        dedup_key=f"message:{message_id}",
        message_id=message_id,
        platform=Platform.INSTAGRAM,
        sender_id=sender_id,
        text=text,
    )


def test_spam_comment_routes_to_spam_check_no_draft(tmp_path, monkeypatch):
    spam_calls = []
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_spam_check", lambda *a, **kw: spam_calls.append(kw) or "ts")
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.SPAM, language=Language.ENGLISH),
        draft=DraftResult(text="should never be used", is_handoff=False),
    )
    ctx = make_ctx(tmp_path, claude)

    process_comment_event(ctx, comment_event(comment_id="spam1", text="DM us to collab!!"))

    assert len(spam_calls) == 1
    assert len(draft_calls) == 0
    assert len(claude.draft_calls) == 0  # draft_reply must never be called for spam comments

    conn = get_connection(ctx.db_path)
    message = get_message(conn, "spam1")
    conn.close()
    assert message.status == MessageStatus.SPAM_PENDING


def test_spam_dm_falls_back_to_handoff_draft(tmp_path, monkeypatch):
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.SPAM, language=Language.ENGLISH),
        draft=DraftResult(text="Thanks! - The Nexoryn Team", is_handoff=True),
    )
    ctx = make_ctx(tmp_path, claude)

    process_message_event(ctx, dm_event(message_id="spamdm1", text="collab offer"))

    assert claude.draft_calls[0]["is_handoff"] is True
    assert len(draft_calls) == 1

    conn = get_connection(ctx.db_path)
    message = get_message(conn, "spamdm1")
    conn.close()
    assert message.status == MessageStatus.PENDING_APPROVAL


def test_pricing_dm_combines_pricing_line_with_handoff(tmp_path, monkeypatch):
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.LEAD, language=Language.ENGLISH, mentions_pricing=True),
        draft=DraftResult(text="Thanks! - The Nexoryn Team", is_handoff=True),
    )
    ctx = make_ctx(tmp_path, claude)

    process_message_event(ctx, dm_event(message_id="pricedm1", text="how much for a website?"))

    assert claude.draft_calls[0]["is_handoff"] is True
    assert len(draft_calls) == 1

    conn = get_connection(ctx.db_path)
    draft_row = conn.execute("SELECT draft_text FROM drafts WHERE message_id = ?", ("pricedm1",)).fetchone()
    conn.close()
    assert draft_row["draft_text"].startswith("Pricing depends on your needs")
    assert "Thanks! - The Nexoryn Team" in draft_row["draft_text"]


def test_pricing_comment_is_alert_only_with_no_draft_row(tmp_path, monkeypatch):
    alert_calls = []
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_alert_only", lambda *a, **kw: alert_calls.append(kw) or "ts")
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.LEAD, language=Language.ENGLISH, mentions_pricing=True),
        draft=DraftResult(text="Thanks! - The Nexoryn Team", is_handoff=True),
    )
    ctx = make_ctx(tmp_path, claude)

    process_comment_event(ctx, comment_event(comment_id="pricecomment1", text="how much for a website?"))

    assert len(alert_calls) == 1
    assert len(draft_calls) == 0

    conn = get_connection(ctx.db_path)
    message = get_message(conn, "pricecomment1")
    draft_row = conn.execute("SELECT * FROM drafts WHERE message_id = ?", ("pricecomment1",)).fetchone()
    conn.close()
    assert message.status == MessageStatus.ALERTED
    assert draft_row is None  # no draft persisted for alert-only comments


def test_model_self_escalation_on_comment_also_routes_to_alert_only(tmp_path, monkeypatch):
    """Even when pipeline didn't pre-decide is_handoff (not pricing, not
    lead), the model can still self-escalate mid-draft (HANDOFF_NEEDED
    sentinel inside claude_client) -- that must also skip public posting
    for comments."""
    alert_calls = []
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_alert_only", lambda *a, **kw: alert_calls.append(kw) or "ts")
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.QUESTION, language=Language.ENGLISH),
        draft=DraftResult(text="Thanks! - The Nexoryn Team", is_handoff=True),  # model self-escalated
    )
    ctx = make_ctx(tmp_path, claude)

    process_comment_event(ctx, comment_event(comment_id="obscure1", text="some off-doc legal question"))

    assert claude.draft_calls[0]["is_handoff"] is False  # pipeline did not request handoff
    assert len(alert_calls) == 1
    assert len(draft_calls) == 0


def test_normal_question_posts_draft_for_approval(tmp_path, monkeypatch):
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.QUESTION, language=Language.ENGLISH),
        draft=DraftResult(text="Yes, we build custom websites!", is_handoff=False),
    )
    ctx = make_ctx(tmp_path, claude)

    process_comment_event(ctx, comment_event(comment_id="q1", text="Do you build websites?"))

    assert len(draft_calls) == 1
    conn = get_connection(ctx.db_path)
    message = get_message(conn, "q1")
    draft = get_draft(conn, 1)
    conn.close()
    assert message.status == MessageStatus.PENDING_APPROVAL
    assert draft.draft_text == "Yes, we build custom websites!"
    assert draft.is_handoff is False


def test_non_english_message_gets_translations(tmp_path, monkeypatch):
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.COMPLIMENT, language=Language.URDU_SCRIPT),
        draft=DraftResult(text="شکریہ!", is_handoff=False),
        translation="Thank you!",
    )
    ctx = make_ctx(tmp_path, claude)

    process_comment_event(ctx, comment_event(comment_id="ur1", text="زبردست کام"))

    assert draft_calls[0]["message_translation"] == "Thank you!"
    assert draft_calls[0]["draft_translation"] == "Thank you!"


def test_duplicate_message_id_is_skipped(tmp_path, monkeypatch):
    draft_calls = []
    monkeypatch.setattr("app.pipeline.post_draft_for_approval", lambda *a, **kw: draft_calls.append(kw) or "ts")

    claude = FakeClaude(
        classification=ClassificationResult(category=Classification.QUESTION, language=Language.ENGLISH),
        draft=DraftResult(text="Yes!", is_handoff=False),
    )
    ctx = make_ctx(tmp_path, claude)

    conn = get_connection(ctx.db_path)
    insert_message(
        conn,
        Message(
            id="dup1",
            platform=Platform.INSTAGRAM,
            type=MessageType.COMMENT,
            sender_id="u1",
            text="already seen",
            received_at="2026-01-01T00:00:00",
        ),
    )
    conn.close()

    process_comment_event(ctx, comment_event(comment_id="dup1", text="already seen"))

    assert len(claude.classify_calls) == 0  # never even classified -- short-circuited before that
    assert len(draft_calls) == 0
