import httpx
import pytest

from app.meta_client import MetaAPIError, MetaClient, verify_webhook_signature


def make_client(handler) -> MetaClient:
    transport = httpx.MockTransport(handler)
    return MetaClient(
        page_id="PAGE123",
        ig_business_id="IG456",
        access_token="test-token",
        base_url="https://graph.facebook.com/v21.0/",
        transport=transport,
    )


def test_get_page_info_returns_parsed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/PAGE123"
        assert request.url.params["access_token"] == "test-token"
        assert request.url.params["fields"] == "name,id"
        return httpx.Response(200, json={"name": "Nexoryn AI", "id": "PAGE123"})

    client = make_client(handler)
    result = client.get_page_info()

    assert result == {"name": "Nexoryn AI", "id": "PAGE123"}


def test_get_ig_account_info_returns_parsed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/IG456"
        return httpx.Response(200, json={"username": "nexoryn.ai", "id": "IG456"})

    client = make_client(handler)
    result = client.get_ig_account_info()

    assert result == {"username": "nexoryn.ai", "id": "IG456"}


def test_reply_to_comment_posts_message():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v21.0/COMMENT1/comments"
        return httpx.Response(200, json={"id": "REPLY1"})

    client = make_client(handler)
    result = client.reply_to_comment("COMMENT1", "Thanks!")

    assert result == {"id": "REPLY1"}


def test_send_dm_posts_json_payload():
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v21.0/PAGE123/messages"
        assert request.url.params["access_token"] == "test-token"
        body = json.loads(request.content)
        assert body == {"recipient": {"id": "USER1"}, "message": {"text": "Hello there"}}
        return httpx.Response(200, json={"message_id": "MSG1"})

    client = make_client(handler)
    result = client.send_dm("USER1", "Hello there")

    assert result == {"message_id": "MSG1"}


def test_get_conversations_lists_page_conversations():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/PAGE123/conversations"
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json={"data": [{"id": "conv1", "updated_time": "2026-01-01T00:00:00+0000"}]})

    client = make_client(handler)
    result = client.get_conversations()

    assert result["data"][0]["id"] == "conv1"


def test_get_comment_replies_lists_replies():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/COMMENT1/comments"
        return httpx.Response(200, json={"data": [{"id": "reply1", "from": {"id": "PAGE123"}}]})

    client = make_client(handler)
    result = client.get_comment_replies("COMMENT1")

    assert result["data"][0]["from"]["id"] == "PAGE123"


def test_get_recent_posts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/PAGE123/posts"
        return httpx.Response(200, json={"data": [{"id": "post1"}]})

    client = make_client(handler)
    result = client.get_recent_posts()

    assert result["data"][0]["id"] == "post1"


def test_get_recent_media():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/IG456/media"
        return httpx.Response(200, json={"data": [{"id": "media1"}]})

    client = make_client(handler)
    result = client.get_recent_media()

    assert result["data"][0]["id"] == "media1"


def test_get_comments_for_post():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/post1/comments"
        return httpx.Response(200, json={"data": []})

    client = make_client(handler)
    client.get_comments_for_post("post1")


def test_get_comments_for_media():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/media1/comments"
        return httpx.Response(200, json={"data": []})

    client = make_client(handler)
    client.get_comments_for_media("media1")


def test_graph_api_error_raises_meta_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid OAuth access token.", "code": 190}},
        )

    client = make_client(handler)

    with pytest.raises(MetaAPIError, match="Invalid OAuth access token"):
        client.get_page_info()


def test_error_object_in_200_response_still_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "Something went wrong"}})

    client = make_client(handler)

    with pytest.raises(MetaAPIError, match="Something went wrong"):
        client.get_page_info()


class TestVerifyWebhookSignature:
    def test_valid_signature_passes(self):
        import hashlib
        import hmac

        secret = "app-secret"
        body = b'{"object":"page","entry":[]}'
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"sha256={digest}"

        assert verify_webhook_signature(body, header, secret) is True

    def test_tampered_body_fails(self):
        import hashlib
        import hmac

        secret = "app-secret"
        digest = hmac.new(secret.encode(), b"original body", hashlib.sha256).hexdigest()
        header = f"sha256={digest}"

        assert verify_webhook_signature(b"tampered body", header, secret) is False

    def test_missing_header_fails(self):
        assert verify_webhook_signature(b"body", None, "secret") is False

    def test_wrong_prefix_fails(self):
        assert verify_webhook_signature(b"body", "sha1=deadbeef", "secret") is False
