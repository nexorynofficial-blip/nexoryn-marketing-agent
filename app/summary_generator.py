"""Analyzes fetched Facebook data (posts, comments, DMs) and produces the
Summary the CLI prints and saves.

"Healthy" is deliberately a narrower check than "do we have any data at
all": only unanswered comments and low-engagement posts count as action
items (matching the JSON example's total_action_items math, which never
counts dm_themes). DM theme categorization is skipped entirely on a
healthy day -- it costs one Claude call per DM, so there's no reason to
pay for it when there's nothing else to review anyway.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple

from app.claude_client import ClaudeClient
from app.models import Comment, DMTheme, LowEngagementPost, Message, Post, Summary, UnansweredComment

logger = logging.getLogger(__name__)

LOW_ENGAGEMENT_THRESHOLD = 5
MAX_UNANSWERED_COMMENTS = 10
MAX_LOW_ENGAGEMENT_POSTS = 5
MAX_DM_THEMES_SHOWN = 4


class SummaryGenerator:
    def __init__(
        self,
        claude: ClaudeClient,
        on_theme_categorized: Optional[Callable[[str, str], None]] = None,
    ):
        self._claude = claude
        # Lets the caller (cli.py) persist a newly-computed DM theme back to
        # the DB so the next run reuses it instead of re-calling Claude.
        self._on_theme_categorized = on_theme_categorized or (lambda message_id, theme: None)

    def analyze(self, posts: List[Post], comments: List[Comment], messages: List[Message]) -> Summary:
        posts_by_id = {post.id: post for post in posts}

        unanswered = self._build_unanswered_comments(comments, posts_by_id)
        low_engagement = self._build_low_engagement_posts(posts)
        total_action_items = len(unanswered) + len(low_engagement)

        if total_action_items == 0:
            return Summary(
                timestamp=_now_iso(),
                status="healthy",
                message="No action items for today. Your Page is healthy!",
            )

        dm_themes = self._build_dm_themes(messages)
        recommendations = self._generate_recommendations(unanswered, low_engagement, dm_themes)

        return Summary(
            timestamp=_now_iso(),
            status="action_needed",
            message=f"Found {total_action_items} item(s) to work on",
            unanswered_comments=unanswered,
            low_engagement_posts=low_engagement,
            dm_themes={theme.name: theme.percentage for theme in dm_themes},
            recommendations=recommendations,
            total_action_items=total_action_items,
        )

    # ---- Part A: unanswered comments ----

    def _build_unanswered_comments(
        self, comments: List[Comment], posts_by_id: Dict[str, Post]
    ) -> List[UnansweredComment]:
        unanswered = sorted((c for c in comments if not c.has_page_reply), key=lambda c: c.created_time, reverse=True)

        result = []
        for comment in unanswered[:MAX_UNANSWERED_COMMENTS]:
            post = posts_by_id.get(comment.post_id)
            post_title = _post_title(post) if post else comment.post_id
            result.append(
                UnansweredComment(
                    post_id=comment.post_id,
                    post_title=post_title,
                    comment_text=comment.text,
                    author=comment.author_name,
                    time_since=_time_since(comment.created_time),
                )
            )
        return result

    # ---- Part B: low-engagement posts ----

    def _build_low_engagement_posts(self, posts: List[Post]) -> List[LowEngagementPost]:
        def score_of(post: Post) -> int:
            return post.engagement_score if post.engagement_score is not None else post.reactions_count + post.comments_count

        low = sorted(
            (p for p in posts if score_of(p) < LOW_ENGAGEMENT_THRESHOLD), key=lambda p: p.created_time, reverse=True
        )

        return [
            LowEngagementPost(
                post_id=post.id,
                title=_post_title(post),
                engagement_score=score_of(post),
                reactions=post.reactions_count,
                comments=post.comments_count,
                posted_ago=_time_since(post.created_time),
            )
            for post in low[:MAX_LOW_ENGAGEMENT_POSTS]
        ]

    # ---- Part C: DM themes ----

    def _build_dm_themes(self, messages: List[Message]) -> List[DMTheme]:
        # Only categorize what customers said -- the Page's own sent
        # messages aren't a "theme" to analyze.
        customer_messages = [m for m in messages if not m.is_from_page]
        if not customer_messages:
            return []

        counts: Counter = Counter()
        for message in customer_messages:
            if message.theme:
                theme = message.theme
            else:
                theme = self._claude.categorize_dm_theme(message.text)
                self._on_theme_categorized(message.id, theme)
            counts[theme] += 1

        total = sum(counts.values())
        return [
            DMTheme(name=name, count=count, percentage=round(count / total * 100))
            for name, count in counts.most_common(MAX_DM_THEMES_SHOWN)
        ]

    # ---- Part D: recommendations ----

    def _generate_recommendations(
        self,
        unanswered: List[UnansweredComment],
        low_engagement: List[LowEngagementPost],
        dm_themes: List[DMTheme],
    ) -> List[str]:
        context_parts = []

        if unanswered:
            lines = "\n".join(
                f'- "{c.comment_text}" by {c.author} on "{c.post_title}" ({c.time_since})' for c in unanswered
            )
            context_parts.append(f"Unanswered Comments ({len(unanswered)}):\n{lines}")

        if low_engagement:
            lines = "\n".join(
                f'- "{p.title}" — engagement score {p.engagement_score} ({p.posted_ago})' for p in low_engagement
            )
            context_parts.append(f"Low-Engagement Posts ({len(low_engagement)}):\n{lines}")

        if dm_themes:
            lines = "\n".join(f"- {theme.name}: {theme.percentage}%" for theme in dm_themes)
            context_parts.append(f"Common DM Themes:\n{lines}")

        return self._claude.generate_recommendations("\n\n".join(context_parts))


def _now_iso() -> str:
    # Naive local time, consistent with every other timestamp in this app
    # (never UTC-labeled) -- no trailing "Z", since that would misrepresent
    # the timezone.
    return dt.datetime.now().isoformat(timespec="seconds")


def _post_title(post: Post) -> str:
    if not post.message:
        return f"Post {post.id}"
    first_line = post.message.strip().splitlines()[0]
    return first_line[:60] + ("…" if len(first_line) > 60 else "")


def _time_since(created_time_iso: str) -> str:
    try:
        created = dt.datetime.fromisoformat(created_time_iso)
    except ValueError:
        return "recently"

    seconds = (dt.datetime.now() - created).total_seconds()
    if seconds < 0:
        return "just now"

    minutes = seconds / 60
    if minutes < 60:
        n = max(1, int(minutes))
        return f"{n} minute{'s' if n != 1 else ''} ago"

    hours = minutes / 60
    if hours < 24:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"

    days = hours / 24
    n = int(days)
    return f"{n} day{'s' if n != 1 else ''} ago"
