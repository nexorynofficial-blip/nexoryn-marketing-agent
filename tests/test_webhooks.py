import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Config
from app.db import init_db
from app.models import Platform
from app.pipeline import CommentEchoEvent, CommentEvent, DmEchoEvent, MessageEvent, PipelineContext
from app.webhooks import _iter_events, _parse_change, _parse_messaging_event, build_webhook_router

OWN_IDS = {"PAGE123", "IG456"}


class TestParseChange:
    def test_instagram_comments_field(self):
        change = {
            "field": "comments",
            "value": {"id": "c1", "text": "Nice work!", "from": {"id": "u1", "username": "jane"}},
        }
        event = _parse_change(change, Platform.INSTAGRAM, OWN_IDS)

        assert isinstance(event, CommentEvent)
        assert event.comment_id == "c1"
        assert event.text == "Nice work!"
        assert event.sender_id == "u1"
        assert event.sender_username == "jane"
        assert event.dedup_key == "comment:c1"

    def test_facebook_feed_comment(self):
        change = {
            "field": "feed",
            "value": {
                "item": "comment",
                "verb": "add",
                "comment_id": "c2",
                "message": "How much?",
                "from": {"id": "u2", "name": "John"},
            },
        }
        event = _parse_change(change, Platform.FACEBOOK, OWN_IDS)

        assert isinstance(event, CommentEvent)
        assert event.comment_id == "c2"
        assert event.sender_username == "John"

    def test_feed_non_comment_item_ignored(self):
        change = {"field": "feed", "value": {"item": "like", "from": {"id": "u3"}}}
        assert _parse_change(change, Platform.FACEBOOK, OWN_IDS) is None

    def test_unknown_field_ignored(self):
        change = {"field": "ratings", "value": {}}
        assert _parse_change(change, Platform.FACEBOOK, OWN_IDS) is None

    def test_own_comment_without_parent_skipped_entirely(self):
        change = {
            "field": "comments",
            "value": {"id": "c4", "text": "our own reply", "from": {"id": "IG456"}},
        }
        assert _parse_change(change, Platform.INSTAGRAM, OWN_IDS) is None

    def test_own_comment_with_parent_becomes_comment_echo_event(self):
        change = {
            "field": "comments",
            "value": {
                "id": "reply1",
                "text": "our own reply",
                "from": {"id": "IG456"},
                "parent": {"id": "original_comment_1"},
            },
        }
        event = _parse_change(change, Platform.INSTAGRAM, OWN_IDS)

        assert isinstance(event, CommentEchoEvent)
        assert event.parent_comment_id == "original_comment_1"
        assert event.dedup_key == "comment_echo:reply1"

    def test_own_facebook_feed_reply_with_parent_id_becomes_echo_event(self):
        change = {
            "field": "feed",
            "value": {
                "item": "comment",
                "verb": "add",
                "comment_id": "reply2",
                "message": "our own reply",
                "from": {"id": "PAGE123"},
                "parent_id": "original_comment_2",
            },
        }
        event = _parse_change(change, Platform.FACEBOOK, OWN_IDS)

        assert isinstance(event, CommentEchoEvent)
        assert event.parent_comment_id == "original_comment_2"

    def test_missing_sender_id_ignored(self):
        change = {"field": "comments", "value": {"id": "c5", "text": "hi", "from": {}}}
        assert _parse_change(change, Platform.INSTAGRAM, OWN_IDS) is None


class TestParseMessagingEvent:
    def test_normal_dm(self):
        event = {"sender": {"id": "u1"}, "message": {"mid": "m1", "text": "hello"}}
        parsed = _parse_messaging_event(event, Platform.INSTAGRAM, OWN_IDS)

        assert isinstance(parsed, MessageEvent)
        assert parsed.message_id == "m1"
        assert parsed.sender_id == "u1"
        assert parsed.dedup_key == "message:m1"

    def test_echo_without_recipient_is_skipped_entirely(self):
        event = {"sender": {"id": "PAGE123"}, "message": {"mid": "m2", "text": "our sent reply", "is_echo": True}}
        assert _parse_messaging_event(event, Platform.FACEBOOK, OWN_IDS) is None

    def test_echo_with_recipient_becomes_dm_echo_event(self):
        event = {
            "sender": {"id": "PAGE123"},
            "recipient": {"id": "customer_1"},
            "message": {"mid": "m2", "text": "our sent reply", "is_echo": True},
        }
        parsed = _parse_messaging_event(event, Platform.FACEBOOK, OWN_IDS)

        assert isinstance(parsed, DmEchoEvent)
        assert parsed.recipient_id == "customer_1"
        assert parsed.dedup_key == "dm_echo:m2"

    def test_own_sender_without_echo_flag_still_skipped(self):
        event = {"sender": {"id": "IG456"}, "message": {"mid": "m3", "text": "x"}}
        assert _parse_messaging_event(event, Platform.INSTAGRAM, OWN_IDS) is None

    def test_non_message_event_ignored(self):
        event = {"sender": {"id": "u1"}, "delivery": {"mids": ["m4"]}}
        assert _parse_messaging_event(event, Platform.INSTAGRAM, OWN_IDS) is None

    def test_missing_text_ignored(self):
        event = {"sender": {"id": "u1"}, "message": {"mid": "m5", "attachments": []}}
        assert _parse_messaging_event(event, Platform.INSTAGRAM, OWN_IDS) is None


class TestIterEvents:
    def test_combines_changes_and_messaging_across_entries(self):
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "id": "IG456",
                    "changes": [
                        {"field": "comments", "value": {"id": "c1", "text": "hi", "from": {"id": "u1"}}}
                    ],
                },
                {
                    "id": "IG456",
                    "messaging": [{"sender": {"id": "u2"}, "message": {"mid": "m1", "text": "hey"}}],
                },
            ],
        }
        events = list(_iter_events(payload, OWN_IDS))

        assert len(events) == 2
        assert isinstance(events[0], CommentEvent)
        assert isinstance(events[1], MessageEvent)

    def test_page_object_maps_to_facebook_platform(self):
        payload = {
            "object": "page",
            "entry": [{"id": "PAGE123", "changes": [
                {"field": "feed", "value": {"item": "comment", "comment_id": "c1", "message": "hi", "from": {"id": "u1"}}}
            ]}],
        }
        events = list(_iter_events(payload, OWN_IDS))

        assert events[0].platform == Platform.FACEBOOK


def make_config(tmp_path) -> Config:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return Config(
        meta_app_id="app-id",
        meta_app_secret="app-secret",
        meta_page_id="PAGE123",
        meta_page_access_token="token",
        meta_ig_business_id="IG456",
        meta_webhook_verify_token="verify-token-123",
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        slack_approvals_channel_id="C123",
        anthropic_api_key="ak-test",
        db_path=db_path,
    )


def make_ctx(config: Config) -> PipelineContext:
    return PipelineContext(
        db_path=config.db_path,
        slack_app=None,
        slack_channel_id=config.slack_approvals_channel_id,
        claude=None,
        knowledge=None,
        meta=None,
    )


def make_client(tmp_path):
    config = make_config(tmp_path)
    ctx = make_ctx(config)
    app = FastAPI()
    app.include_router(build_webhook_router(config, ctx))
    return TestClient(app), config


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestWebhookRouter:
    def test_verify_challenge_with_correct_token(self, tmp_path):
        client, config = make_client(tmp_path)
        r = client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": config.meta_webhook_verify_token, "hub.challenge": "xyz"},
        )
        assert r.status_code == 200
        assert r.text == "xyz"

    def test_verify_challenge_with_wrong_token_rejected(self, tmp_path):
        client, config = make_client(tmp_path)
        r = client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "xyz"},
        )
        assert r.status_code == 403

    def test_post_without_valid_signature_rejected(self, tmp_path):
        client, config = make_client(tmp_path)
        body = b'{"object": "instagram", "entry": []}'
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bogus"})
        assert r.status_code == 403

    def test_post_with_non_json_body_rejected(self, tmp_path):
        client, config = make_client(tmp_path)
        body = b"not json"
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, config.meta_app_secret)})
        assert r.status_code == 400

    def test_post_dispatches_comment_event_to_pipeline(self, tmp_path, monkeypatch):
        client, config = make_client(tmp_path)
        calls = []
        monkeypatch.setattr("app.webhooks.process_comment_event", lambda ctx, event: calls.append(event))

        payload = {
            "object": "instagram",
            "entry": [{
                "id": "IG456",
                "changes": [{"field": "comments", "value": {"id": "c1", "text": "hi", "from": {"id": "u1"}}}],
            }],
        }
        body = json.dumps(payload).encode()
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, config.meta_app_secret)})

        assert r.status_code == 200
        assert len(calls) == 1
        assert calls[0].comment_id == "c1"

    def test_duplicate_event_processed_only_once(self, tmp_path, monkeypatch):
        client, config = make_client(tmp_path)
        calls = []
        monkeypatch.setattr("app.webhooks.process_comment_event", lambda ctx, event: calls.append(event))

        payload = {
            "object": "instagram",
            "entry": [{
                "id": "IG456",
                "changes": [{"field": "comments", "value": {"id": "c1", "text": "hi", "from": {"id": "u1"}}}],
            }],
        }
        body = json.dumps(payload).encode()
        headers = {"X-Hub-Signature-256": sign(body, config.meta_app_secret)}

        client.post("/webhook", content=body, headers=headers)
        client.post("/webhook", content=body, headers=headers)  # simulates a Meta retry

        assert len(calls) == 1

    def test_pipeline_exception_does_not_break_the_ack(self, tmp_path, monkeypatch):
        client, config = make_client(tmp_path)

        def boom(ctx, event):
            raise RuntimeError("something failed")

        monkeypatch.setattr("app.webhooks.process_comment_event", boom)

        payload = {
            "object": "instagram",
            "entry": [{
                "id": "IG456",
                "changes": [{"field": "comments", "value": {"id": "c1", "text": "hi", "from": {"id": "u1"}}}],
            }],
        }
        body = json.dumps(payload).encode()
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, config.meta_app_secret)})

        assert r.status_code == 200

    def test_dm_echo_event_dispatches_to_handle_dm_echo(self, tmp_path, monkeypatch):
        client, config = make_client(tmp_path)
        calls = []
        monkeypatch.setattr("app.webhooks.handle_dm_echo", lambda ctx, recipient_id: calls.append(recipient_id))

        payload = {
            "object": "instagram",
            "entry": [{
                "id": "IG456",
                "messaging": [{
                    "sender": {"id": "PAGE123"},
                    "recipient": {"id": "customer_1"},
                    "message": {"mid": "m1", "text": "reply", "is_echo": True},
                }],
            }],
        }
        body = json.dumps(payload).encode()
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, config.meta_app_secret)})

        assert r.status_code == 200
        assert calls == ["customer_1"]

    def test_comment_echo_event_dispatches_to_handle_comment_echo(self, tmp_path, monkeypatch):
        client, config = make_client(tmp_path)
        calls = []
        monkeypatch.setattr("app.webhooks.handle_comment_echo", lambda ctx, parent_comment_id: calls.append(parent_comment_id))

        payload = {
            "object": "instagram",
            "entry": [{
                "id": "IG456",
                "changes": [{
                    "field": "comments",
                    "value": {
                        "id": "reply1",
                        "text": "our reply",
                        "from": {"id": "IG456"},
                        "parent": {"id": "original_comment_1"},
                    },
                }],
            }],
        }
        body = json.dumps(payload).encode()
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, config.meta_app_secret)})

        assert r.status_code == 200
        assert calls == ["original_comment_1"]
