import pytest

from app.knowledge import (
    DEFAULT_KNOWLEDGE_PATH,
    Knowledge,
    KnowledgeFormatError,
    _extract_handoff_messages,
    _extract_pricing_line,
    load_knowledge,
)
from app.models import Language

SAMPLE_DOC = """
# Some Agency

## FAQs

| Question | Approved answer |
| --- | --- |
| What services do you offer? | We do things. |
| How much do you charge? | Pricing depends on your needs, so let's talk on a call. (fixed policy) |

## Fixed messages

### Handoff message

*(DMs the agent can't answer)*

**English:** Thanks for reaching out! We've informed our team. – The Team

**Urdu script:** شکریہ! ہم نے ٹیم کو بتا دیا ہے۔ – ٹیم

**Roman Urdu:** Shukriya! Humne team ko bata diya hai. – The Team

### Comments

No fixed message here.

## Escalation rules

Some other content.
"""


def test_extract_handoff_messages_from_sample_doc():
    messages = _extract_handoff_messages(SAMPLE_DOC)

    assert messages[Language.ENGLISH] == "Thanks for reaching out! We've informed our team. – The Team"
    assert messages[Language.URDU_SCRIPT].startswith("شکریہ")
    assert messages[Language.URDU_ROMAN].startswith("Shukriya")


def test_missing_handoff_section_raises_clear_error():
    with pytest.raises(KnowledgeFormatError, match="Handoff message"):
        _extract_handoff_messages("# Doc with no handoff section at all")


def test_missing_one_language_raises_clear_error():
    broken_doc = """
### Handoff message

**English:** Thanks! – The Team

### Comments
"""
    with pytest.raises(KnowledgeFormatError, match="Urdu script"):
        _extract_handoff_messages(broken_doc)


def test_load_knowledge_real_file_parses_without_error():
    """Smoke test against the actual agency_knowledge.md the user maintains --
    confirms their real document still matches the expected handoff/pricing format."""
    if not DEFAULT_KNOWLEDGE_PATH.exists():
        pytest.skip("knowledge/agency_knowledge.md not found")

    knowledge = load_knowledge()

    assert isinstance(knowledge, Knowledge)
    assert len(knowledge.raw_text) > 0
    for language in (Language.ENGLISH, Language.URDU_SCRIPT, Language.URDU_ROMAN):
        assert knowledge.handoff_messages[language].strip() != ""
    assert knowledge.pricing_line.strip() != ""


def test_extract_pricing_line_from_sample_doc():
    pricing_line = _extract_pricing_line(SAMPLE_DOC)

    assert pricing_line == "Pricing depends on your needs, so let's talk on a call."
    assert "(fixed policy)" not in pricing_line


def test_missing_pricing_line_raises_clear_error():
    with pytest.raises(KnowledgeFormatError, match="pricing"):
        _extract_pricing_line("# Doc with no FAQ table at all")


def test_system_block_has_cache_control():
    knowledge = Knowledge(raw_text="hello", handoff_messages={}, pricing_line="")
    block = knowledge.system_block()

    assert block["type"] == "text"
    assert block["text"] == "hello"
    assert block["cache_control"] == {"type": "ephemeral"}
