"""Meta Graph API client for the CLI: posts, comments, and inbox messages
from Nexoryn's own Facebook Page. Only a Page access token is needed --
the page's own id is resolved at runtime via GET /me (the token itself is
page-scoped), so there's no separate configured Page ID anymore.

A lightweight file-based cache (.cache/*.json) avoids hammering the Graph
API on repeated runs: each fetch is cached under the TTL below, and
`force=True` bypasses it. This is a CLI tool run on demand or every couple
of hours, not a server, so a simple file cache is enough -- no Redis, no
in-memory process to keep warm.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from app.config import Config
from app.models import Comment, Message, Post

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(".cache")
CACHE_TTL_MINUTES = 30


class MetaAPIError(RuntimeError):
    def __init__(self, status_code: int, error_body: Dict[str, Any]):
        self.status_code = status_code
        self.error_body = error_body
        message = error_body.get("error", {}).get("message", str(error_body))
        super().__init__(f"Meta Graph API error ({status_code}): {message}")


def _handle_response(resp: requests.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not resp.ok or "error" in data:
        logger.error("meta_api_error", extra={"status": resp.status_code, "body": data})
        raise MetaAPIError(resp.status_code, data)
    return data


def _parse_fb_timestamp(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    # Compared against naive cutoffs, consistent with timestamps stored as
    # naive local-time ISO strings everywhere else in this app.
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


class _FileCache:
    """Caches a Graph API response's raw JSON under `.cache/<key>.json`,
    alongside the time it was fetched. A read older than `ttl_minutes` is
    treated as a miss."""

    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR, ttl_minutes: int = CACHE_TTL_MINUTES):
        self.cache_dir = cache_dir
        self.ttl = dt.timedelta(minutes=ttl_minutes)

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, key: str) -> Optional[Any]:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = dt.datetime.fromisoformat(payload["fetched_at"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return None
        if dt.datetime.now() - fetched_at > self.ttl:
            return None
        return payload["data"]

    def set(self, key: str, data: Any) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "data": data}
        self._path(key).write_text(json.dumps(payload), encoding="utf-8")


class MetaClient:
    def __init__(
        self,
        *,
        access_token: str,
        base_url: str = "https://graph.facebook.com/v21.0/",
        cache: Optional[_FileCache] = None,
    ):
        self._token = access_token
        self._base = base_url
        self._cache = cache if cache is not None else _FileCache()
        self._page_id: Optional[str] = None

    @classmethod
    def from_config(cls, config: Config) -> "MetaClient":
        return cls(access_token=config.meta_page_access_token, base_url=config.graph_api_base())

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        params["access_token"] = self._token
        resp = requests.get(f"{self._base}{path}", params=params, timeout=15)
        return _handle_response(resp)

    def _cached_get(self, cache_key: str, path: str, params: Dict[str, Any], force: bool) -> Dict[str, Any]:
        if not force:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.info("meta_cache_hit", extra={"cache_key": cache_key})
                return cached
        data = self._get(path, params)
        self._cache.set(cache_key, data)
        return data

    def _get_page_id(self, force: bool = False) -> str:
        if self._page_id is not None:
            return self._page_id
        data = self._cached_get("me", "me", {"fields": "id,name"}, force)
        self._page_id = data["id"]
        return self._page_id

    # ---- the 3 methods ----

    def get_posts_since(self, hours: int, force: bool = False) -> List[Post]:
        cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
        data = self._cached_get(
            f"posts_{hours}h",
            "me/posts",
            {
                "fields": "id,message,created_time,permalink_url,"
                "reactions.summary(true).limit(0),comments.summary(true).limit(0)",
                "since": int(cutoff.timestamp()),
                "limit": 100,
            },
            force,
        )

        posts: List[Post] = []
        now = dt.datetime.now().isoformat(timespec="seconds")
        for item in data.get("data", []):
            created_time = _parse_fb_timestamp(item.get("created_time"))
            if created_time is None or created_time < cutoff:
                continue
            reactions_count = item.get("reactions", {}).get("summary", {}).get("total_count", 0)
            comments_count = item.get("comments", {}).get("summary", {}).get("total_count", 0)
            posts.append(
                Post(
                    id=item["id"],
                    message=item.get("message", ""),
                    created_time=created_time.isoformat(timespec="seconds"),
                    reactions_count=reactions_count,
                    comments_count=comments_count,
                    permalink_url=item.get("permalink_url"),
                    engagement_score=reactions_count + comments_count,
                    fetched_at=now,
                )
            )
        return posts

    def get_comments_on_posts(self, posts: List[Post], force: bool = False) -> List[Comment]:
        page_id = self._get_page_id(force=force)
        now = dt.datetime.now().isoformat(timespec="seconds")

        all_comments: List[Comment] = []
        for post in posts:
            try:
                data = self._cached_get(
                    f"comments_{post.id}",
                    f"{post.id}/comments",
                    {"fields": "id,message,from,created_time,comments{from}", "filter": "toplevel", "limit": 50},
                    force,
                )
            except MetaAPIError:
                logger.exception("get_comments_failed", extra={"post_id": post.id})
                continue

            for item in data.get("data", []):
                replies = item.get("comments", {}).get("data", [])
                has_page_reply = any(reply.get("from", {}).get("id") == page_id for reply in replies)
                sender = item.get("from", {})
                created_time = _parse_fb_timestamp(item.get("created_time")) or dt.datetime.now()
                all_comments.append(
                    Comment(
                        id=item["id"],
                        post_id=post.id,
                        text=item.get("message", ""),
                        author_name=sender.get("name", "Unknown"),
                        author_id=sender.get("id"),
                        created_time=created_time.isoformat(timespec="seconds"),
                        has_page_reply=has_page_reply,
                        fetched_at=now,
                    )
                )
        return all_comments

    def get_inbox_messages(self, hours: int, force: bool = False) -> List[Message]:
        page_id = self._get_page_id(force=force)
        cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
        data = self._cached_get(
            f"conversations_{hours}h",
            "me/conversations",
            {"fields": "id,updated_time,messages{id,message,from,created_time}", "limit": 50},
            force,
        )

        all_messages: List[Message] = []
        now = dt.datetime.now().isoformat(timespec="seconds")
        for conv in data.get("data", []):
            conv_id = conv.get("id")
            for item in conv.get("messages", {}).get("data", []):
                created_time = _parse_fb_timestamp(item.get("created_time"))
                if created_time is None or created_time < cutoff:
                    continue
                sender = item.get("from", {})
                sender_id = sender.get("id")
                all_messages.append(
                    Message(
                        id=item["id"],
                        conversation_id=conv_id,
                        sender_id=sender_id or "unknown",
                        sender_name=sender.get("name"),
                        text=item.get("message", ""),
                        created_time=created_time.isoformat(timespec="seconds"),
                        is_from_page=(sender_id == page_id),
                        fetched_at=now,
                    )
                )
        return all_messages
