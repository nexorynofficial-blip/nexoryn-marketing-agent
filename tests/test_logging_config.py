import json
import logging

from app.logging_config import configure_logging


def test_log_record_renders_as_json_with_context(capsys):
    configure_logging(level="INFO")
    logger = logging.getLogger("app.test_module_a")

    logger.info("classification_done", extra={"message_id": "abc123", "category": "question"})

    captured = json.loads(capsys.readouterr().out.strip())
    assert captured["event"] == "classification_done"
    assert captured["message_id"] == "abc123"
    assert captured["category"] == "question"
    assert captured["logger"] == "app.test_module_a"
    assert captured["level"] == "info"
    assert "timestamp" in captured


def test_exception_is_rendered_as_readable_traceback(capsys):
    configure_logging(level="INFO")
    logger = logging.getLogger("app.test_module_b")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("something_failed")

    captured = json.loads(capsys.readouterr().out.strip())
    assert captured["event"] == "something_failed"
    assert "ValueError: boom" in captured["exception"]


def test_secrets_are_not_logged_by_default(capsys):
    """Sanity check: this logging setup renders whatever fields callers
    pass, so it's on call sites to avoid passing secrets -- but a plain
    log call with no extra data must never leak anything beyond the
    message itself."""
    configure_logging(level="INFO")
    logger = logging.getLogger("app.test_module_c")

    logger.info("token_check_ok")

    captured = json.loads(capsys.readouterr().out.strip())
    assert set(captured.keys()) == {"event", "logger", "level", "timestamp"}


def test_file_sink_receives_the_same_json(tmp_path):
    log_file = tmp_path / "agent.log"
    configure_logging(level="INFO", log_file_path=str(log_file))
    logger = logging.getLogger("app.test_module_d")

    logger.info("file_sink_test", extra={"draft_id": 7})

    # Close handlers so Windows releases the file handle before we read it.
    for handler in logging.getLogger().handlers:
        handler.close()

    content = log_file.read_text().strip()
    captured = json.loads(content)
    assert captured["event"] == "file_sink_test"
    assert captured["draft_id"] == 7


def test_level_filters_below_threshold(capsys):
    configure_logging(level="WARNING")
    logger = logging.getLogger("app.test_module_e")

    logger.info("should_not_appear")
    logger.warning("should_appear")

    out = capsys.readouterr().out
    assert "should_not_appear" not in out
    assert "should_appear" in out
