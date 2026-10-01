import pytest

from app.config import Config, mask_secret


def test_from_env_succeeds_with_both_vars(monkeypatch):
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key456")

    config = Config.from_env()

    assert config.meta_page_access_token == "token123"
    assert config.anthropic_api_key == "key456"
    assert config.db_path == "nexoryn_agent.db"
    assert config.log_file_path is None


def test_from_env_raises_clear_error_naming_missing_var(monkeypatch):
    monkeypatch.delenv("META_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key456")

    with pytest.raises(RuntimeError, match="META_PAGE_ACCESS_TOKEN"):
        Config.from_env()


def test_from_env_raises_naming_all_missing_vars(monkeypatch):
    monkeypatch.delenv("META_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        Config.from_env()

    assert "META_PAGE_ACCESS_TOKEN" in str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_graph_api_base_uses_configured_version(monkeypatch):
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v22.0")

    config = Config.from_env()

    assert config.graph_api_base() == "https://graph.facebook.com/v22.0/"


def test_mask_secret_keeps_only_first_and_last_chars():
    assert mask_secret("abcdefghijklmnop") == "abcd...mnop"


def test_mask_secret_empty_string():
    assert mask_secret("") == ""
