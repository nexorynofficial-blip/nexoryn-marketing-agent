"""Wraps Anthropic (Claude) calls: combined classification + language
detection, reply drafting (including fixed handoff lookups), and English
translation for Slack display.

Uses prompt caching: the agency knowledge file is sent as its own system
content block with cache_control set (see app.knowledge.Knowledge), so
repeated calls within Anthropic's cache TTL only pay full input-token price
once. The knowledge block is always placed first in `system` so later,
task-specific instructions (which differ between classify/draft calls)
don't break the cache prefix match.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import anthropic

from app.knowledge import Knowledge
from app.models import Classification, Language

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

# The model is instructed to emit this exact token when it can't answer
# confidently from the knowledge file, so draft_reply() can fall back to the
# fixed handoff message instead of letting it guess.
_HANDOFF_SENTINEL = "HANDOFF_NEEDED"


@dataclass(frozen=True)
class ClassificationResult:
    category: Classification
    language: Language
    # Whether the message asks about price/cost/rates/packages, in any
    # language. A hardcoded English keyword list would miss Urdu-script and
    # Roman-Urdu pricing questions, so this rides along on the classification
    # call itself rather than a local heuristic in pipeline.py.
    mentions_pricing: bool = False


@dataclass(frozen=True)
class ConversationTurn:
    """One prior message in a DM thread, used as drafting context."""

    sender: str  # "user" or "page"
    text: str


@dataclass(frozen=True)
class DraftResult:
    text: str
    is_handoff: bool


class ClaudeClient:
    def __init__(self, api_key: str, knowledge: Knowledge, model: str = MODEL):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._knowledge = knowledge
        self._model = model

    # ---- classification + language detection (combined call) ----

    def classify_message(self, message_text: str) -> ClassificationResult:
        instructions = (
            "You are a classification assistant for Nexoryn's social media inbox. "
            "Using the agency knowledge document above as context, read the message "
            "below and respond with STRICT JSON ONLY (no markdown, no commentary), "
            "in exactly this shape:\n"
            '{"category": "<one of: lead, question, compliment, complaint, spam, other>", '
            '"language": "<one of: en, ur_script, ur_roman, other>", '
            '"mentions_pricing": <true or false>}\n\n'
            "category: what kind of message this is.\n"
            "language: en for English, ur_script for Urdu written in Urdu script, "
            "ur_roman for Urdu written in Latin/Roman letters, other for anything else.\n"
            "mentions_pricing: true if the message asks about price, cost, rates, "
            "packages, or budget, in ANY language or script -- false otherwise.\n\n"
            f"Message:\n{message_text}"
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=80,
            system=[self._knowledge.system_block(), {"type": "text", "text": instructions}],
            messages=[{"role": "user", "content": "Classify the message above."}],
            extra_body={"temperature": 0},
        )

        return _parse_classification_json(_extract_text(response))

    def detect_language(self, message_text: str) -> Language:
        """Convenience wrapper around classify_message(). Prefer
        classify_message() directly when you also need the category, since
        this makes its own full API call and discards the category."""
        return self.classify_message(message_text).language

    # ---- reply drafting ----

    def draft_reply(
        self,
        message_text: str,
        classification: Classification,
        language: Language,
        is_handoff: bool,
        conversation_history: Optional[Sequence[ConversationTurn]] = None,
    ) -> DraftResult:
        """Returns the reply to send.

        If `is_handoff` is True (pipeline already decided this needs the
        team, e.g. pricing/meeting requests), this returns the FIXED handoff
        string from the knowledge file verbatim -- no model call, since that
        wording must never be paraphrased. If the model itself decides mid-
        draft that it can't answer confidently, it also falls back to the
        fixed handoff text (see _HANDOFF_SENTINEL) rather than guessing.
        """
        if is_handoff:
            return DraftResult(text=self._handoff_text(language), is_handoff=True)

        history_block = ""
        if conversation_history:
            lines = [f"{turn.sender}: {turn.text}" for turn in conversation_history]
            history_block = "Recent conversation history (oldest first):\n" + "\n".join(lines) + "\n\n"

        instructions = (
            "You are drafting a reply for Nexoryn's Instagram/Facebook inbox, using "
            "the agency knowledge document above as your ONLY source of fact. Follow "
            "its tone-of-voice rules (formality, reply length, emoji rules, language "
            "style) exactly.\n\n"
            "Hard rules:\n"
            "- Never state or imply specific prices, discounts, or timelines.\n"
            "- Never promise results, outcomes, or guarantees.\n"
            "- Never discuss competitors or name/discuss other clients.\n"
            '- Always sign off as "The Nexoryn Team", never claim to be a specific person.\n'
            "- If asked directly whether this is AI: answer honestly that replies are "
            "drafted with AI help and reviewed by the team before sending.\n"
            "- If you cannot answer confidently and completely from the knowledge "
            f'document, respond with exactly "{_HANDOFF_SENTINEL}" and nothing else -- '
            "do not guess.\n"
            f"- Reply in this language/script: {language.value}\n\n"
            f"{history_block}"
            f"Message classification: {classification.value}\n"
            f"Message to reply to:\n{message_text}\n\n"
            "Write only the reply text, nothing else."
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=300,
            system=[self._knowledge.system_block(), {"type": "text", "text": instructions}],
            messages=[{"role": "user", "content": "Draft the reply."}],
            extra_body={"temperature": 0.4},
        )

        reply = _extract_text(response).strip()
        if reply == _HANDOFF_SENTINEL:
            return DraftResult(text=self._handoff_text(language), is_handoff=True)

        return DraftResult(text=reply, is_handoff=False)

    def _handoff_text(self, language: Language) -> str:
        return self._knowledge.handoff_messages.get(
            language, self._knowledge.handoff_messages[Language.ENGLISH]
        )

    # ---- translation for Slack display ----

    def translate_to_english(self, text: str) -> str:
        instructions = (
            "Translate the following message to natural English. Respond with "
            "ONLY the translation, no commentary, no quotes.\n\n"
            f"Message:\n{text}"
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=300,
            system=[{"type": "text", "text": instructions}],
            messages=[{"role": "user", "content": "Translate."}],
            extra_body={"temperature": 0},
        )

        return _extract_text(response).strip()


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "".join(parts)


def _parse_classification_json(raw: str) -> ClassificationResult:
    """Parses the model's classification JSON defensively: malformed or
    unexpected output falls back to (other, other) and logs a warning,
    rather than raising and taking down the pipeline over one bad response.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        category = Classification(data["category"])
        language = Language(data["language"])
        mentions_pricing = bool(data.get("mentions_pricing", False))
        return ClassificationResult(category=category, language=language, mentions_pricing=mentions_pricing)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning(
            "classification_parse_failed",
            extra={"raw_response": raw[:200], "error": str(exc)},
        )
        return ClassificationResult(category=Classification.OTHER, language=Language.OTHER, mentions_pricing=False)
