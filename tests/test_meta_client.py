import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from app.meta_client import MetaAPIError, MetaClient, _FileCache


def make_response(json_data, status_code=200, ok=True):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.ok = ok
    return resp


def make_client(tmp_path) -> MetaClient:
    cache = _FileCache(cache_dir=tmp_path / ".cache")
    return MetaClient(access_token="test-token", base_url="https://graph.facebook.com/v21.0/", cache=cache)


class TestFileCache:
    def test_miss_when_nothing_cached(self, tmp_path):
        cache = _FileCache(cache_dir=tmp_path / ".cache")
        assert cache.get("key1") is None

    def test_hit_after_set(self, tmp_path):
        cache = _FileCache(cache_dir=tmp_path / ".cache")
        cache.set("key1", {"data": [1, 2, 3]})
        assert cache.get("key1") == {"data": [1, 2, 3]}

    def test_expired_entry_is_a_miss(self, tmp_path):
        cache = _FileCache(cache_dir=tmp_path / ".cache", ttl_minutes=30)
        cache.set("key1", {"data": "x"})

        # Backdate the cached fetched_at past the TTL.
        import json

        path = cache._path("key1")
        payload = json.loads(path.read_text())
        payload["fetched_at"] = (dt.datetime.now() - dt.timedelta(minutes=31)).isoformat(timespec="seconds")
        path.write_text(json.dumps(payload))

        assert cache.get("key1") is None


class TestGetPostsSince:
    def test_fetches_and_filters_by_window(self, tmp_path):
        now = dt.datetime.now()
        recent = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        old = (now - dt.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S+0000")

        response_data = {
            "data": [
                {
                    "id": "p1",
                    "message": "New product launch!",
                    "created_time": recent,
                    "reactions": {"summary": {"total_count": 10}},
                    "comments": {"summary": {"total_count": 3}},
                },
                {
                    "id": "p2",
                    "message": "Old post",
                    "created_time": old,
                    "reactions": {"summary": {"total_count": 1}},
                    "comments": {"summary": {"total_count": 0}},
                },
            ]
        }

        client = make_client(tmp_path)
        with patch("app.meta_client.requests.get", return_value=make_response(response_data)) as mock_get:
            posts = client.get_posts_since(hours=24)

        assert len(posts) == 1
        assert posts[0].id == "p1"
        assert posts[0].engagement_score == 13
        mock_get.assert_called_once()

    def test_uses_cache_on_second_call(self, tmp_path):
        response_data = {"data": []}
        client = make_client(tmp_path)
        with patch("app.meta_client.requests.get", return_value=make_response(response_data)) as mock_get:
            client.get_posts_since(hours=24)
            client.get_posts_since(hours=24)

        assert mock_get.call_count == 1

    def test_force_bypasses_cache(self, tmp_path):
        response_data = {"data": []}
        client = make_client(tmp_path)
        with patch("app.meta_client.requests.get", return_value=make_response(response_data)) as mock_get:
            client.get_posts_since(hours=24)
            client.get_posts_since(hours=24, force=True)

        assert mock_get.call_count == 2


class TestGetCommentsOnPosts:
    def test_detects_page_reply(self, tmp_path):
        from app.models import Post

        me_response = {"id": "PAGE123", "name": "Nexoryn"}
        comments_response = {
            "data": [
                {
                    "id": "c1",
                    "message": "Is this available in Europe?",
                    "from": {"id": "u1", "name": "Sarah M."},
                    "created_time": "2026-10-01T10:00:00+0000",
                    "comments": {"data": [{"from": {"id": "PAGE123"}}]},
                },
                {
                    "id": "c2",
                    "message": "Nice!",
                    "from": {"id": "u2", "name": "Alex"},
                    "created_time": "2026-10-01T10:05:00+0000",
                    "comments": {"data": []},
                },
            ]
        }

        client = make_client(tmp_path)
        with patch("app.meta_client.requests.get") as mock_get:
            mock_get.side_effect = [make_response(me_response), make_response(comments_response)]
            comments = client.get_comments_on_posts([Post(id="post1", message="x", created_time="2026-10-01T09:00:00")])

        by_id = {c.id: c for c in comments}
        assert by_id["c1"].has_page_reply is True
        assert by_id["c2"].has_page_reply is False

    def test_one_failing_post_does_not_stop_others(self, tmp_path):
        from app.models import Post

        me_response = {"id": "PAGE123"}
        good_response = {
            "data": [
                {"id": "c1", "message": "hi", "from": {"id": "u1", "name": "A"}, "created_time": "2026-10-01T10:00:00+0000", "comments": {"data": []}}
            ]
        }

        client = make_client(tmp_path)
        with patch("app.meta_client.requests.get") as mock_get:
            mock_get.side_effect = [
                make_response(me_response),
                make_response({"error": {"message": "bad post"}}, status_code=400, ok=False),
                make_response(good_response),
            ]
            posts = [Post(id="bad_post", message="x", created_time="2026-10-01T09:00:00"), Post(id="good_post", message="x", created_time="2026-10-01T09:00:00")]
            comments = client.get_comments_on_posts(posts)

        assert len(comments) == 1
        assert comments[0].id == "c1"


class TestGetInboxMessages:
    def test_filters_by_window_and_flags_page_sender(self, tmp_path):
        now = dt.datetime.now()
        recent = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")

        me_response = {"id": "PAGE123"}
        conversations_response = {
            "data": [
                {
                    "id": "conv1",
                    "messages": {
                        "data": [
                            {"id": "m1", "message": "When will it ship?", "from": {"id": "u1", "name": "John"}, "created_time": recent},
                            {"id": "m2", "message": "Thanks for reaching out!", "from": {"id": "PAGE123"}, "created_time": recent},
                        ]
                    },
                }
            ]
        }

        client = make_client(tmp_path)
        with patch("app.meta_client.requests.get") as mock_get:
            mock_get.side_effect = [make_response(me_response), make_response(conversations_response)]
            messages = client.get_inbox_messages(hours=24)

        by_id = {m.id: m for m in messages}
        assert by_id["m1"].is_from_page is False
        assert by_id["m2"].is_from_page is True


class TestErrorHandling:
    def test_error_response_raises_meta_api_error(self, tmp_path):
        client = make_client(tmp_path)
        with patch("app.meta_client.requests.get", return_value=make_response({"error": {"message": "Invalid token"}}, status_code=401, ok=False)):
            with pytest.raises(MetaAPIError, match="Invalid token"):
                client.get_posts_since(hours=24)
