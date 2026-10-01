"""Loads and validates environment configuration for the Nexoryn Agent CLI.

Only two credentials are needed now that this is a local, on-demand CLI
tool (no webhooks, no Slack, no server) -- fails fast with a clear error
naming exactly which one is missing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS = [
    "META_PAGE_ACCESS_TOKEN",
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
    meta_page_access_token: str
    anthropic_api_key: str

    meta_graph_api_version: str = "v21.0"
    db_path: str = "nexoryn_agent.db"
    log_level: str = "INFO"
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
            meta_page_access_token=os.environ["META_PAGE_ACCESS_TOKEN"],
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            meta_graph_api_version=os.environ.get("META_GRAPH_API_VERSION") or "v21.0",
            db_path=os.environ.get("DB_PATH") or "nexoryn_agent.db",
            log_level=os.environ.get("LOG_LEVEL") or "INFO",
            log_file_path=os.environ.get("LOG_FILE_PATH") or None,
        )

    def graph_api_base(self) -> str:
        return f"https://graph.facebook.com/{self.meta_graph_api_version}/"


def load_config() -> Config:
    return Config.from_env()
