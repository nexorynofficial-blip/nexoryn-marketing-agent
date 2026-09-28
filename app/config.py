"""Loads and validates environment configuration for the Nexoryn Social Agent.

Fails fast (at Config.from_env() call time) so a missing credential surfaces
as one clear error naming exactly which variable is unset, instead of a
confusing failure deep inside a webhook handler or Slack callback later.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS = [
    "META_APP_ID",
    "META_APP_SECRET",
    "META_PAGE_ID",
    "META_PAGE_ACCESS_TOKEN",
    "META_IG_BUSINESS_ID",
    "META_WEBHOOK_VERIFY_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_APPROVALS_CHANNEL_ID",
    "ANTHROPIC_API_KEY",
]


def mask_secret(value: str, visible: int = 4) -> str:
    """Masks a secret for safe logging, keeping only the first/last `visible` chars."""
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


@dataclass(frozen=True)
class Config:
    meta_app_id: str
    meta_app_secret: str
    meta_page_id: str
    meta_page_access_token: str
    meta_ig_business_id: str
    meta_webhook_verify_token: str
    slack_bot_token: str
    slack_app_token: str
    slack_approvals_channel_id: str
    anthropic_api_key: str

    meta_graph_api_version: str = "v21.0"
    db_path: str = "nexoryn_agent.db"
    log_level: str = "INFO"
    port: int = 8000
    # Set only in production (e.g. /var/log/nexoryn-agent/agent.log on the
    # VM); left unset for local dev, which just logs to stdout.
    log_file_path: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill these in."
            )

        return cls(
            meta_app_id=os.environ["META_APP_ID"],
            meta_app_secret=os.environ["META_APP_SECRET"],
            meta_page_id=os.environ["META_PAGE_ID"],
            meta_page_access_token=os.environ["META_PAGE_ACCESS_TOKEN"],
            meta_ig_business_id=os.environ["META_IG_BUSINESS_ID"],
            meta_webhook_verify_token=os.environ["META_WEBHOOK_VERIFY_TOKEN"],
            slack_bot_token=os.environ["SLACK_BOT_TOKEN"],
            slack_app_token=os.environ["SLACK_APP_TOKEN"],
            slack_approvals_channel_id=os.environ["SLACK_APPROVALS_CHANNEL_ID"],
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            meta_graph_api_version=os.environ.get("META_GRAPH_API_VERSION") or "v21.0",
            db_path=os.environ.get("DB_PATH") or "nexoryn_agent.db",
            log_level=os.environ.get("LOG_LEVEL") or "INFO",
            port=int(os.environ.get("PORT") or "8000"),
            log_file_path=os.environ.get("LOG_FILE_PATH") or None,
        )

    def graph_api_base(self) -> str:
        return f"https://graph.facebook.com/{self.meta_graph_api_version}/"


def load_config() -> Config:
    return Config.from_env()
