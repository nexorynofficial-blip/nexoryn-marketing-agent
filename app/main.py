"""FastAPI app instance: runs startup backfill, connects the Slack Bolt app
(Socket Mode) alongside the webhooks router, and schedules the echo-poll
and draft-expiry/nudge background jobs.

Startup order matters: backfill runs synchronously in the FastAPI startup
hook, which uvicorn waits on before serving any traffic (including
webhooks) -- so a backfilled message can never race a live webhook for the
same comment/DM. Slack Socket Mode connects after backfill completes, for
the same reason.

Uses SocketModeHandler.connect() rather than .start(): .start() blocks the
calling thread AND registers a SIGINT handler, which only works on the
main thread of the main interpreter -- fine for a script that does nothing
else, but it crashed here because uvicorn owns the main thread's signal
handling. .connect() does the same handshake and then returns, since the
socket read loop runs in its own background threads internally; it's safe
to call directly from FastAPI's startup hook.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.claude_client import ClaudeClient
from app.config import load_config
from app.db import init_db
from app.dedupe import run_comment_echo_poll, run_expiry_nudge_check
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
own_ids = {config.meta_page_id, config.meta_ig_business_id}


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
scheduler = BackgroundScheduler()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
def _startup() -> None:
    from scripts.backfill import run_backfill

    count = run_backfill(pipeline_ctx, own_ids)
    logger.info("startup_backfill_complete", extra={"processed": count})

    socket_mode_handler.connect()
    logger.info("slack_socket_mode_connected")

    scheduler.add_job(
        lambda: run_expiry_nudge_check(pipeline_ctx),
        "interval",
        minutes=10,
        id="draft_expiry_nudge",
    )
    scheduler.add_job(
        lambda: run_comment_echo_poll(pipeline_ctx, own_ids),
        "interval",
        minutes=10,
        id="comment_echo_poll",
    )
    scheduler.start()
    logger.info("scheduler_started")


@app.on_event("shutdown")
def _shutdown() -> None:
    scheduler.shutdown(wait=False)
    socket_mode_handler.close()
    logger.info("shutdown_complete")
