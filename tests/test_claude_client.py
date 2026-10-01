from types import SimpleNamespace
from unittest.mock import MagicMock

from app.claude_client import ClaudeClient, _parse_numbered_list


def make_client_with_mocked_response(response_text: str) -> ClaudeClient:
    client = ClaudeClient(api_key="test-key")
    fake_response = SimpleNamespace(content=[SimpleNamespace(type="text", text=response_text)])
    client._client = MagicMock()
    client._client.messages.create.return_value = fake_response
    return client


class TestCategorizeDmTheme:
    def test_returns_matching_theme(self):
        client = make_client_with_mocked_response("Shipping inquiries")
        assert client.categorize_dm_theme("Where is my order?") == "Shipping inquiries"

    def test_strips_whitespace(self):
        client = make_client_with_mocked_response("  Product questions  \n")
        assert client.categorize_dm_theme("Does this come in blue?") == "Product questions"

    def test_unrecognized_theme_falls_back_to_other(self):
        client = make_client_with_mocked_response("Something Claude made up")
        assert client.categorize_dm_theme("hmm") == "Other"

    def test_api_failure_falls_back_to_other_without_raising(self):
        client = ClaudeClient(api_key="test-key")
        client._client = MagicMock()
        client._client.messages.create.side_effect = RuntimeError("API down")

        assert client.categorize_dm_theme("anything") == "Other"

    def test_uses_extra_body_for_temperature(self):
        client = make_client_with_mocked_response("Other")
        client.categorize_dm_theme("test")

        call_kwargs = client._client.messages.create.call_args.kwargs
        assert "temperature" not in call_kwargs
        assert call_kwargs["extra_body"] == {"temperature": 0}


class TestGenerateRecommendations:
    def test_parses_numbered_list(self):
        client = make_client_with_mocked_response(
            "1. Reply to Sarah's question within 1 hour\n2. Pin the new post\n3. Respond to 3 comments"
        )

        result = client.generate_recommendations("some context")

        assert result == [
            "Reply to Sarah's question within 1 hour",
            "Pin the new post",
            "Respond to 3 comments",
        ]

    def test_api_failure_returns_empty_list(self):
        client = ClaudeClient(api_key="test-key")
        client._client = MagicMock()
        client._client.messages.create.side_effect = RuntimeError("API down")

        assert client.generate_recommendations("context") == []


class TestParseNumberedList:
    def test_handles_various_markers(self):
        raw = "1. First\n2) Second\n- Third\n* Fourth\n• Fifth"
        assert _parse_numbered_list(raw) == ["First", "Second", "Third", "Fourth", "Fifth"]

    def test_skips_blank_lines(self):
        raw = "1. First\n\n2. Second\n   \n3. Third"
        assert _parse_numbered_list(raw) == ["First", "Second", "Third"]

    def test_empty_string_returns_empty_list(self):
        assert _parse_numbered_list("") == []
