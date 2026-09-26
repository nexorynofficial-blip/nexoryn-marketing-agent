"""FastAPI app instance: mounts the webhooks router and starts the Slack
Bolt app (Socket Mode, in a background thread) alongside it.

APScheduler jobs (echo detection, draft expiry/nudge) are added here in
Phase E.
"""
from __future__ import annotations

import logging
import threading

from fastapi import FastAPI
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.claude_client import ClaudeClient
from app.config import load_config
from app.db import init_db
from app.knowledge import load_knowledge
from app.meta_client import MetaClient
from app.pipeline import PipelineContext
from app.slack_app import SendCallbacks, build_slack_app
from app.webhooks import build_webhook_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

config = load_config()
init_db(config.db_path)

knowledge = load_knowledge()
claude = ClaudeClient(api_key=config.anthropic_api_key, knowledge=knowledge)
meta = MetaClient.from_config(config)


def _send_comment_reply(comment_id: str, text: str) -> None:
    meta.reply_to_comment(comment_id, text)


def _send_dm_reply(recipient_id: str, text: str) -> None:
    meta.send_dm(recipient_id, text)


def _hide_comment(comment_id: str) -> None:
    meta.hide_comment(comment_id, hide=True)


send_callbacks = SendCallbacks(
    send_comment_reply=_send_comment_reply,
    send_dm_reply=_send_dm_reply,
    hide_comment=_hide_comment,
)

slack_app = build_slack_app(bot_token=config.slack_bot_token, db_path=config.db_path, send_callbacks=send_callbacks)

pipeline_ctx = PipelineContext(
    db_path=config.db_path,
    slack_app=slack_app,
    slack_channel_id=config.slack_approvals_channel_id,
    claude=claude,
    knowledge=knowledge,
    meta=meta,
)

app = FastAPI(title="Nexoryn Social Agent")
app.include_router(build_webhook_router(config, pipeline_ctx))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
def _start_slack_socket_mode() -> None:
    handler = SocketModeHandler(slack_app, config.slack_app_token)
    thread = threading.Thread(target=handler.start, daemon=True)
    thread.start()
    logger.info("slack_socket_mode_started")
