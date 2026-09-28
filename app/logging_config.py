"""Structured JSON logging setup, shared by main.py and any script that
wants the same format (verify_setup.py, backfill.py, etc. can opt in too).

Wires structlog into Python's standard logging so every existing
`logging.getLogger(__name__).info(...)` / `.exception(..., extra={...})`
call across the codebase renders as a JSON line -- no call-site changes
needed anywhere in meta_client.py, claude_client.py, slack_app.py,
webhooks.py, pipeline.py, dedupe.py, or scripts/backfill.py.

stdout is always a sink (systemd captures it via journald with no extra
config). A second sink at `log_file_path` is added only when one is
configured (production, via LOG_FILE_PATH in .env) -- local dev leaves
this unset and just gets stdout.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

import structlog


def configure_logging(level: str = "INFO", log_file_path: Optional[str] = None) -> None:
    # Every existing logger call site in this codebase uses plain stdlib
    # `logging.getLogger(__name__)` -- nothing calls structlog.get_logger()
    # directly. Those records are "foreign" to structlog, so the enrichment
    # (logger name, level, timestamp, folding `extra={...}` in, formatting
    # exceptions) has to run via `foreign_pre_chain` on the formatter, not
    # just the processors passed to structlog.configure(). Sharing the same
    # list for both keeps native structlog calls (if ever added) consistent
    # with everything else.
    foreign_pre_chain = [
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=foreign_pre_chain + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    if log_file_path:
        # A plain FileHandler (not RotatingFileHandler) is deliberate: log
        # rotation on the VM is handled externally by logrotate, configured
        # with `copytruncate` specifically so this process never needs to
        # reopen the file after a rotation.
        file_handler = logging.FileHandler(log_file_path)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
