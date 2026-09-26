"""Loads knowledge/agency_knowledge.md and prepares it for prompt caching.

The whole file is handed to Claude as a single cached system content block —
Claude reads the markdown (tables, headings, prose) natively, so most of the
document is never parsed here. The one exception is the three fixed handoff
messages (English / Urdu script / Roman Urdu), which are sent verbatim and
must never be rewritten by the model, so they're extracted structurally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Union

from app.models import Language

DEFAULT_KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "agency_knowledge.md"

# Matches the "### Handoff message" section, stopping at the next ## or ###
# heading. DOTALL so `.` spans newlines within the section.
_HANDOFF_SECTION_RE = re.compile(r"###\s*Handoff message(.*?)(?=\n#{2,3}\s|\Z)", re.DOTALL)

# Each handoff message is expected on a single line as "**Label:** text".
_LABEL_PATTERNS = {
    Language.ENGLISH: re.compile(r"\*\*English:\*\*\s*(.+)"),
    Language.URDU_SCRIPT: re.compile(r"\*\*Urdu script:\*\*\s*(.+)"),
    Language.URDU_ROMAN: re.compile(r"\*\*Roman Urdu:\*\*\s*(.+)"),
}

_LABEL_NAMES = {
    Language.ENGLISH: "English",
    Language.URDU_SCRIPT: "Urdu script",
    Language.URDU_ROMAN: "Roman Urdu",
}


class KnowledgeFormatError(ValueError):
    """Raised when agency_knowledge.md doesn't match the expected handoff
    message format. Fails loudly on purpose: a silently-missing handoff
    string would mean DMs get an empty or wrong reply."""


def _extract_handoff_messages(text: str) -> Dict[Language, str]:
    section_match = _HANDOFF_SECTION_RE.search(text)
    if not section_match:
        raise KnowledgeFormatError(
            "Could not find a '### Handoff message' section in agency_knowledge.md."
        )
    section = section_match.group(1)

    messages: Dict[Language, str] = {}
    for language, pattern in _LABEL_PATTERNS.items():
        match = pattern.search(section)
        if not match:
            raise KnowledgeFormatError(
                f"Could not find a handoff message for '{_LABEL_NAMES[language]}' in "
                "the '### Handoff message' section of agency_knowledge.md. Expected "
                f"a single line like '**{_LABEL_NAMES[language]}:** <message text>'."
            )
        messages[language] = match.group(1).strip()

    return messages


@dataclass(frozen=True)
class Knowledge:
    raw_text: str
    handoff_messages: Dict[Language, str]

    def system_block(self) -> dict:
        """Anthropic system content block with prompt caching enabled.

        Anthropic's prompt cache keys on an exact-prefix match, so this
        block's text must be byte-identical across calls to get a cache hit.
        Load one Knowledge object per process at startup and reuse it for
        every request rather than re-reading the file each time.
        """
        return {
            "type": "text",
            "text": self.raw_text,
            "cache_control": {"type": "ephemeral"},
        }


def load_knowledge(path: Union[str, Path] = DEFAULT_KNOWLEDGE_PATH) -> Knowledge:
    text = Path(path).read_text(encoding="utf-8")
    handoff_messages = _extract_handoff_messages(text)
    return Knowledge(raw_text=text, handoff_messages=handoff_messages)
