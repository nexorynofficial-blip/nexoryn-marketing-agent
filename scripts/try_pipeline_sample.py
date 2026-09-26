"""Manual smoke test for Phase B.

Classifies and drafts replies for a few hardcoded sample messages using the
real Anthropic API and your real agency_knowledge.md -- no Slack, no
webhooks, no database writes. This is the first place you'll see the
knowledge file's tone-of-voice rules actually shape a reply.

Run with the built-in samples:
    python scripts/try_pipeline_sample.py

Or pass your own message(s) to try instead:
    python scripts/try_pipeline_sample.py "Do you build shopify stores?"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.claude_client import ClaudeClient
from app.config import load_config
from app.knowledge import load_knowledge
from app.models import Language

SAMPLE_MESSAGES = [
    "Do you guys build automations for CRMs?",
    "How much would a website cost?",
    "زبردست کام! بہت پسند آیا۔",
    "DM us to collab and grow your followers fast!!",
]


def run_one(client: ClaudeClient, text: str) -> None:
    print(f"\n--- Message: {text!r} ---")

    result = client.classify_message(text)
    print(f"Classification : {result.category.value}")
    print(f"Language       : {result.language.value}")

    # Crude standalone stand-in for the escalation rules pipeline.py will
    # implement in Phase D -- good enough to exercise both drafting paths here.
    needs_handoff = any(word in text.lower() for word in ("cost", "price", "quote"))

    draft = client.draft_reply(
        message_text=text,
        classification=result.category,
        language=result.language,
        is_handoff=needs_handoff,
    )
    print(f"Is handoff     : {draft.is_handoff}")
    print(f"Draft          : {draft.text}")

    if result.language != Language.ENGLISH:
        print(f"Original (EN)  : {client.translate_to_english(text)}")
        print(f"Draft (EN)     : {client.translate_to_english(draft.text)}")


def main() -> None:
    config = load_config()
    knowledge = load_knowledge()
    client = ClaudeClient(api_key=config.anthropic_api_key, knowledge=knowledge)

    messages = sys.argv[1:] or SAMPLE_MESSAGES
    for text in messages:
        run_one(client, text)


if __name__ == "__main__":
    main()
