from app.models import Comment, Message, Post
from app.summary_generator import LOW_ENGAGEMENT_THRESHOLD, MAX_UNANSWERED_COMMENTS, SummaryGenerator


class FakeClaude:
    def __init__(self, theme="Other", recommendations=None):
        self.theme = theme
        self.recommendations = recommendations if recommendations is not None else ["Do thing 1", "Do thing 2"]
        self.categorize_calls = []
        self.recommend_calls = []

    def categorize_dm_theme(self, text):
        self.categorize_calls.append(text)
        return self.theme

    def generate_recommendations(self, context):
        self.recommend_calls.append(context)
        return self.recommendations


def make_post(**overrides) -> Post:
    defaults = dict(
        id="post-1",
        message="New product launch!",
        created_time="2026-10-01T10:00:00",
        reactions_count=10,
        comments_count=5,
        engagement_score=15,
    )
    defaults.update(overrides)
    return Post(**defaults)


def make_comment(**overrides) -> Comment:
    defaults = dict(
        id="c1",
        post_id="post-1",
        text="Is this available in Europe?",
        author_name="Sarah M.",
        created_time="2026-10-01T10:15:00",
        has_page_reply=False,
    )
    defaults.update(overrides)
    return Comment(**defaults)


def make_message(**overrides) -> Message:
    defaults = dict(
        id="m1",
        conversation_id="conv-1",
        sender_id="u1",
        text="When will my order ship?",
        created_time="2026-10-01T11:00:00",
        is_from_page=False,
    )
    defaults.update(overrides)
    return Message(**defaults)


class TestHealthyPath:
    def test_no_unanswered_or_low_engagement_is_healthy(self):
        claude = FakeClaude()
        generator = SummaryGenerator(claude)

        result = generator.analyze(
            posts=[make_post(reactions_count=100, comments_count=50, engagement_score=150)],
            comments=[make_comment(has_page_reply=True)],
            messages=[make_message()],
        )

        assert result.status == "healthy"
        assert result.total_action_items == 0
        assert result.unanswered_comments == []
        assert result.low_engagement_posts == []
        assert result.recommendations == []

    def test_healthy_path_never_calls_claude(self):
        """DM theme categorization costs one Claude call per message -- must
        be skipped entirely when there's nothing else to review."""
        claude = FakeClaude()
        generator = SummaryGenerator(claude)

        generator.analyze(
            posts=[make_post(reactions_count=100, comments_count=50, engagement_score=150)],
            comments=[make_comment(has_page_reply=True)],
            messages=[make_message()],
        )

        assert claude.categorize_calls == []
        assert claude.recommend_calls == []

    def test_no_data_at_all_is_healthy(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(posts=[], comments=[], messages=[])
        assert result.status == "healthy"


class TestUnansweredComments:
    def test_unanswered_included_answered_excluded(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(
            posts=[make_post()],
            comments=[
                make_comment(id="c1", has_page_reply=False),
                make_comment(id="c2", has_page_reply=True),
            ],
            messages=[],
        )

        assert [c.comment_text for c in result.unanswered_comments] == ["Is this available in Europe?"]

    def test_sorted_newest_first(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(
            posts=[make_post()],
            comments=[
                make_comment(id="c1", created_time="2026-10-01T08:00:00", text="older"),
                make_comment(id="c2", created_time="2026-10-01T10:00:00", text="newer"),
            ],
            messages=[],
        )

        assert [c.comment_text for c in result.unanswered_comments] == ["newer", "older"]

    def test_limited_to_max(self):
        comments = [
            make_comment(id=f"c{i}", created_time=f"2026-10-01T10:{i:02d}:00") for i in range(MAX_UNANSWERED_COMMENTS + 5)
        ]
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(posts=[make_post()], comments=comments, messages=[])

        assert len(result.unanswered_comments) == MAX_UNANSWERED_COMMENTS

    def test_post_title_resolved_from_post_message(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(
            posts=[make_post(id="p1", message="New product launch!")],
            comments=[make_comment(post_id="p1")],
            messages=[],
        )
        assert result.unanswered_comments[0].post_title == "New product launch!"

    def test_missing_post_falls_back_to_post_id(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(
            posts=[],  # post not fetched/stored, only the comment is
            comments=[make_comment(post_id="unknown-post")],
            messages=[],
        )
        assert result.unanswered_comments[0].post_title == "unknown-post"


class TestLowEngagementPosts:
    def test_below_threshold_included(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(
            posts=[make_post(reactions_count=2, comments_count=0, engagement_score=2)],
            comments=[],
            messages=[],
        )
        assert len(result.low_engagement_posts) == 1
        assert result.low_engagement_posts[0].engagement_score == 2

    def test_at_or_above_threshold_excluded(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(
            posts=[make_post(reactions_count=3, comments_count=2, engagement_score=LOW_ENGAGEMENT_THRESHOLD)],
            comments=[],
            messages=[],
        )
        assert result.low_engagement_posts == []
        assert result.status == "healthy"

    def test_falls_back_to_computing_score_when_not_precomputed(self):
        generator = SummaryGenerator(FakeClaude())
        result = generator.analyze(
            posts=[make_post(reactions_count=1, comments_count=1, engagement_score=None)],
            comments=[],
            messages=[],
        )
        assert result.low_engagement_posts[0].engagement_score == 2


class TestDmThemes:
    def test_categorizes_only_customer_messages(self):
        claude = FakeClaude(theme="Order status")
        generator = SummaryGenerator(claude)

        generator.analyze(
            posts=[make_post(reactions_count=0, comments_count=0, engagement_score=0)],
            comments=[],
            messages=[
                make_message(id="m1", is_from_page=False, text="customer msg"),
                make_message(id="m2", is_from_page=True, text="our own reply"),
            ],
        )

        assert claude.categorize_calls == ["customer msg"]

    def test_reuses_precomputed_theme_without_calling_claude(self):
        claude = FakeClaude()
        generator = SummaryGenerator(claude)

        generator.analyze(
            posts=[make_post(reactions_count=0, comments_count=0, engagement_score=0)],
            comments=[],
            messages=[make_message(theme="Shipping inquiries")],
        )

        assert claude.categorize_calls == []

    def test_persists_newly_categorized_theme_via_callback(self):
        saved = []
        claude = FakeClaude(theme="Bug reports")
        generator = SummaryGenerator(claude, on_theme_categorized=lambda mid, theme: saved.append((mid, theme)))

        generator.analyze(
            posts=[make_post(reactions_count=0, comments_count=0, engagement_score=0)],
            comments=[],
            messages=[make_message(id="m1")],
        )

        assert saved == [("m1", "Bug reports")]

    def test_theme_percentages_in_output(self):
        claude = FakeClaude(theme="Shipping inquiries")
        generator = SummaryGenerator(claude)

        result = generator.analyze(
            posts=[make_post(reactions_count=0, comments_count=0, engagement_score=0)],
            comments=[],
            messages=[make_message(id=f"m{i}") for i in range(4)],
        )

        assert result.dm_themes == {"Shipping inquiries": 100}


class TestRecommendations:
    def test_generated_when_action_needed(self):
        claude = FakeClaude(recommendations=["Reply to Sarah", "Pin the post"])
        generator = SummaryGenerator(claude)

        result = generator.analyze(posts=[make_post()], comments=[make_comment()], messages=[])

        assert result.recommendations == ["Reply to Sarah", "Pin the post"]
        assert len(claude.recommend_calls) == 1
        assert "Unanswered Comments" in claude.recommend_calls[0]

    def test_total_action_items_excludes_dm_themes(self):
        """Matches the spec's own JSON example: total_action_items counts
        only unanswered comments + low-engagement posts, never dm_themes."""
        claude = FakeClaude(theme="Product questions")
        generator = SummaryGenerator(claude)

        result = generator.analyze(
            posts=[make_post(reactions_count=100, comments_count=100, engagement_score=200)],
            comments=[make_comment(has_page_reply=False)],
            messages=[make_message(id="m1"), make_message(id="m2")],
        )

        assert result.total_action_items == 1
        assert result.status == "action_needed"
