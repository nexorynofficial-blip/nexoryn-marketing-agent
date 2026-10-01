"""Wraps Anthropic (Claude) calls for the CLI: categorizing DM themes and
generating engagement recommendations from a day's Facebook activity.

No knowledge-file prompt caching here (unlike the old reply-drafting
flow) -- there's no fixed agency document to ground these calls in, just
the data gathered for this run.
"""
from __future__ import annotations

import logging
import re
from typing import List

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

DM_THEMES = [
    "Shipping inquiries",
    "Product questions",
    "Order status",
    "General feedback",
    "Bug reports",
    "Other",
]


class ClaudeClient:
    def __init__(self, api_key: str, model: str = MODEL):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def categorize_dm_theme(self, message_text: str) -> str:
        """Classifies one DM into a fixed theme bucket. Falls back to
        "Other" on any parse/API issue, rather than letting one bad
        message crash an entire summary run."""
        instructions = (
            "Categorize this Facebook DM into exactly one of these themes:\n"
            + "\n".join(f"- {theme}" for theme in DM_THEMES)
            + "\n\nRespond with ONLY the theme name, exactly as written above, nothing else.\n\n"
            f"Message:\n{message_text}"
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=20,
                system=instructions,
                messages=[{"role": "user", "content": "Categorize the message above."}],
                extra_body={"temperature": 0},
            )
            theme = _extract_text(response).strip()
        except Exception:  # noqa: BLE001
            logger.exception("categorize_dm_theme_failed")
            return "Other"

        return theme if theme in DM_THEMES else "Other"

    def generate_recommendations(self, context: str) -> List[str]:
        """Takes a pre-formatted block describing unanswered comments,
        low-engagement posts, and DM themes, and returns a short list of
        specific, actionable recommendations. Returns [] (not a crash) if
        the API call itself fails -- callers already only call this when
        there's real data to work with."""
        instructions = (
            "Based on this Facebook Page data, provide 3-5 SPECIFIC, ACTIONABLE "
            "steps to increase engagement TODAY. Be concise. Include time estimates.\n\n"
            f"{context}\n\n"
            "Return ONLY the numbered list of actions. No introductions or explanations."
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system=instructions,
                messages=[{"role": "user", "content": "Generate the recommendations."}],
                extra_body={"temperature": 0.4},
            )
        except Exception:  # noqa: BLE001
            logger.exception("generate_recommendations_failed")
            return []

        return _parse_numbered_list(_extract_text(response))


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "".join(parts)


_LIST_MARKER_RE = re.compile(r"^\s*(\d+[.)]|[-*•])\s*")


def _parse_numbered_list(raw: str) -> List[str]:
    """Parses "1. Do X\\n2. Do Y" into ["Do X", "Do Y"] -- strips numbering
    or bullet markers, drops blank lines, never raises on odd formatting."""
    items = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = _LIST_MARKER_RE.sub("", line).strip()
        if cleaned:
            items.append(cleaned)
    return items
