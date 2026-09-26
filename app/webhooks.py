"""FastAPI router: Meta webhook verify challenge, POST event receiver,
signature validation, per-item dedup, and dispatch into pipeline.py.

Parses two payload shapes per Meta's Webhooks Reference:
  - "changes" entries with field "comments" (Instagram) or "feed" (Facebook
    Page) -- new comments.
  - "messaging" entries (shared shape for Messenger and Instagram Direct)
    -- new DMs.

NOTE: the exact payload shapes here are built from Meta's documented
webhook formats but have not yet been exercised against real deliveries.
Parsing is deliberately defensive (unrecognized/malformed items are logged
and skipped, never raised) so one odd event can't take down the whole
request -- but the shapes themselves should be double-checked against the
first real test events once the cloudflared tunnel is live.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, Optional, Set, Union

from fastapi import APIRouter, Request, Response

from app.config import Config
from app.db import get_connection
from app.dedupe import is_duplicate_webhook_event, mark_webhook_event_processed
from app.meta_client import verify_webhook_signature
from app.models import Platform
from app.pipeline import CommentEvent, MessageEvent, PipelineContext, process_comment_event, process_message_event

logger = logging.getLogger(__name__)

Event = Union[CommentEvent, MessageEvent]


def build_webhook_router(config: Config, ctx: PipelineContext) -> APIRouter:
    router = APIRouter()
    own_ids = {config.meta_page_id, config.meta_ig_business_id}

    @router.get("/webhook")
    def verify_subscription(request: Request) -> Response:
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge", "")

        if mode == "subscribe" and token == config.meta_webhook_verify_token:
            return Response(content=challenge, media_type="text/plain")

        logger.warning("webhook_verify_failed", extra={"mode": mode})
        return Response(status_code=403)

    @router.post("/webhook")
    async def receive_event(request: Request) -> Response:
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")

        if not verify_webhook_signature(body, signature, config.meta_app_secret):
            logger.warning("webhook_signature_invalid")
            return Response(status_code=403)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("webhook_payload_not_json")
            return Response(status_code=400)

        conn = get_connection(config.db_path)
        try:
            for event in _iter_events(payload, own_ids):
                if is_duplicate_webhook_event(conn, event.dedup_key):
                    logger.info("webhook_event_duplicate", extra={"dedup_key": event.dedup_key})
                    continue
                mark_webhook_event_processed(conn, event.dedup_key)

                try:
                    if isinstance(event, CommentEvent):
                        process_comment_event(ctx, event)
                    else:
                        process_message_event(ctx, event)
                except Exception:  # noqa: BLE001 - one bad event must never break the ack or other events
                    logger.exception("event_processing_failed", extra={"dedup_key": event.dedup_key})
        finally:
            conn.close()

        # Meta expects a fast 200 regardless of internal outcome; retries on
        # anything else, which would just reprocess (harmlessly, thanks to
        # dedup) or pile up.
        return Response(status_code=200)

    return router


def _iter_events(payload: Dict[str, Any], own_ids: Set[str]) -> Iterator[Event]:
    object_type = payload.get("object")
    platform = Platform.FACEBOOK if object_type == "page" else Platform.INSTAGRAM

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            event = _parse_change(change, platform, own_ids)
            if event is not None:
                yield event
        for messaging_event in entry.get("messaging", []):
            event = _parse_messaging_event(messaging_event, platform, own_ids)
            if event is not None:
                yield event


def _parse_change(change: Dict[str, Any], platform: Platform, own_ids: Set[str]) -> Optional[CommentEvent]:
    field = change.get("field")
    value = change.get("value", {})

    if field == "comments":
        comment_id = value.get("id")
        text = value.get("text", "")
        sender = value.get("from", {})
        sender_id = sender.get("id")
        sender_username = sender.get("username")
    elif field == "feed" and value.get("item") == "comment" and value.get("verb", "add") == "add":
        comment_id = value.get("comment_id")
        text = value.get("message", "")
        sender = value.get("from", {})
        sender_id = sender.get("id")
        sender_username = sender.get("name")
    else:
        return None

    if not comment_id or not sender_id:
        logger.warning("webhook_comment_missing_fields", extra={"field": field})
        return None

    if sender_id in own_ids:
        # Our own reply (sent by this bot, or by a human via Business Suite)
        # -- never draft a reply to ourselves.
        return None

    return CommentEvent(
        dedup_key=f"comment:{comment_id}",
        comment_id=comment_id,
        platform=platform,
        sender_id=sender_id,
        sender_username=sender_username,
        text=text,
    )


def _parse_messaging_event(event: Dict[str, Any], platform: Platform, own_ids: Set[str]) -> Optional[MessageEvent]:
    message = event.get("message")
    if not message:
        # Read receipts, postbacks, etc. -- not a text message to process.
        return None

    if message.get("is_echo"):
        # Our own sent message (or a human teammate's, via Business Suite)
        # being echoed back. Phase E uses these to cancel pending drafts;
        # for now, just don't treat it as a new incoming message.
        return None

    mid = message.get("mid")
    text = message.get("text")
    sender_id = event.get("sender", {}).get("id")

    if not mid or not text or not sender_id:
        return None

    if sender_id in own_ids:
        return None

    return MessageEvent(
        dedup_key=f"message:{mid}",
        message_id=mid,
        platform=platform,
        sender_id=sender_id,
        text=text,
    )
