from types import SimpleNamespace
from unittest.mock import MagicMock

from app.claude_client import ClaudeClient, ConversationTurn, _parse_classification_json
from app.knowledge import Knowledge
from app.models import Classification, Language


def make_knowledge() -> Knowledge:
    return Knowledge(
        raw_text="# Nexoryn knowledge doc\nSome agency facts here.",
        handoff_messages={
            Language.ENGLISH: "Thanks for reaching out! – The Nexoryn Team",
            Language.URDU_SCRIPT: "شکریہ! – ٹیم",
            Language.URDU_ROMAN: "Shukriya! – The Nexoryn Team",
        },
    )


def make_client_with_mocked_response(response_text: str) -> ClaudeClient:
    client = ClaudeClient(api_key="test-key", knowledge=make_knowledge())
    fake_response = SimpleNamespace(content=[SimpleNamespace(type="text", text=response_text)])
    client._client = MagicMock()
    client._client.messages.create.return_value = fake_response
    return client


class TestClassifyMessage:
    def test_parses_valid_json(self):
        client = make_client_with_mocked_response('{"category": "question", "language": "en"}')

        result = client.classify_message("Do you build websites?")

        assert result.category == Classification.QUESTION
        assert result.language == Language.ENGLISH

    def test_handles_json_wrapped_in_code_fence(self):
        client = make_client_with_mocked_response('```json\n{"category": "lead", "language": "ur_roman"}\n```')

        result = client.classify_message("Mujhe website chahiye")

        assert result.category == Classification.LEAD
        assert result.language == Language.URDU_ROMAN

    def test_malformed_json_falls_back_to_other_without_raising(self):
        client = make_client_with_mocked_response("not json at all")

        result = client.classify_message("some message")

        assert result.category == Classification.OTHER
        assert result.language == Language.OTHER

    def test_unknown_enum_value_falls_back_to_other(self):
        client = make_client_with_mocked_response('{"category": "banana", "language": "en"}')

        result = client.classify_message("some message")

        assert result.category == Classification.OTHER
        assert result.language == Language.OTHER

    def test_detect_language_returns_only_language(self):
        client = make_client_with_mocked_response('{"category": "compliment", "language": "ur_script"}')

        language = client.detect_language("زبردست کام")

        assert language == Language.URDU_SCRIPT


class TestDraftReply:
    def test_is_handoff_true_returns_fixed_text_with_no_api_call(self):
        client = make_client_with_mocked_response("should not be used")

        result = client.draft_reply(
            message_text="How much does a website cost?",
            classification=Classification.LEAD,
            language=Language.ENGLISH,
            is_handoff=True,
        )

        assert result.is_handoff is True
        assert result.text == "Thanks for reaching out! – The Nexoryn Team"
        client._client.messages.create.assert_not_called()

    def test_handoff_sentinel_from_model_falls_back_to_fixed_text(self):
        client = make_client_with_mocked_response("HANDOFF_NEEDED")

        result = client.draft_reply(
            message_text="Some obscure question",
            classification=Classification.QUESTION,
            language=Language.ENGLISH,
            is_handoff=False,
        )

        assert result.is_handoff is True
        assert result.text == "Thanks for reaching out! – The Nexoryn Team"

    def test_normal_draft_returns_model_text(self):
        client = make_client_with_mocked_response("Yes, we build custom websites!")

        result = client.draft_reply(
            message_text="Do you build websites?",
            classification=Classification.QUESTION,
            language=Language.ENGLISH,
            is_handoff=False,
        )

        assert result.is_handoff is False
        assert result.text == "Yes, we build custom websites!"

    def test_conversation_history_is_included_in_the_prompt(self):
        client = make_client_with_mocked_response("Sure, happy to help!")

        client.draft_reply(
            message_text="Any updates?",
            classification=Classification.QUESTION,
            language=Language.ENGLISH,
            is_handoff=False,
            conversation_history=[
                ConversationTurn(sender="user", text="Hi, I messaged earlier"),
                ConversationTurn(sender="page", text="Thanks, we'll follow up"),
            ],
        )

        call_kwargs = client._client.messages.create.call_args.kwargs
        instructions_text = call_kwargs["system"][1]["text"]
        assert "Hi, I messaged earlier" in instructions_text
        assert "Thanks, we'll follow up" in instructions_text

    def test_knowledge_block_is_first_system_block_for_cache_prefix(self):
        client = make_client_with_mocked_response("Some reply")

        client.draft_reply(
            message_text="Do you build websites?",
            classification=Classification.QUESTION,
            language=Language.ENGLISH,
            is_handoff=False,
        )

        call_kwargs = client._client.messages.create.call_args.kwargs
        first_block = call_kwargs["system"][0]
        assert first_block["cache_control"] == {"type": "ephemeral"}
        assert first_block["text"] == client._knowledge.raw_text


class TestTranslateToEnglish:
    def test_returns_model_text(self):
        client = make_client_with_mocked_response("Thank you very much")

        result = client.translate_to_english("Bohat shukriya")

        assert result == "Thank you very much"


class TestParseClassificationJsonDirectly:
    def test_missing_keys_falls_back_gracefully(self):
        result = _parse_classification_json('{"category": "spam"}')

        assert result.category == Classification.OTHER
        assert result.language == Language.OTHER
