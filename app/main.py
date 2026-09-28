"""FastAPI app instance: mounts the webhooks router and connects the Slack
Bolt app (Socket Mode) alongside it, in the same process and thread.

Uses SocketModeHandler.connect() rather than .start(): .start() blocks the
calling thread AND registers a SIGINT handler, which only works on the
main thread of the main interpreter -- fine for a script that does nothing
else, but it crashed here because uvicorn owns the main thread's signal
handling. .connect() does the same handshake and then returns, since the
socket read loop runs in its own background threads internally; it's safe
to call directly from FastAPI's startup hook.

APScheduler jobs (echo detection, draft expiry/nudge) are added here in
Phase E.
"""
from __future__ import annotations

import logging

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

socket_mode_handler = SocketModeHandler(slack_app, config.slack_app_token)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
def _connect_slack_socket_mode() -> None:
    socket_mode_handler.connect()
    logger.info("slack_socket_mode_connected")


@app.on_event("shutdown")
def _close_slack_socket_mode() -> None:
    socket_mode_handler.close()
    logger.info("slack_socket_mode_closed")
