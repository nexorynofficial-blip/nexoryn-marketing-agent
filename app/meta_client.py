"""Meta Graph API client: page/IG info, comments, DMs, hiding, webhook signatures.

All calls go through `_get`/`_post` so error handling and access-token
attachment happen in one place. Errors from the Graph API (including HTTP
200 responses that still carry an `error` object) raise `MetaAPIError`
rather than failing silently.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

import httpx

from app.config import Config

logger = logging.getLogger(__name__)


class MetaAPIError(RuntimeError):
    def __init__(self, status_code: int, error_body: Dict[str, Any]):
        self.status_code = status_code
        self.error_body = error_body
        message = error_body.get("error", {}).get("message", str(error_body))
        super().__init__(f"Meta Graph API error ({status_code}): {message}")


def _handle_response(resp: httpx.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = {}

    if resp.is_error or "error" in data:
        logger.error("meta_api_error", extra={"status": resp.status_code, "body": data})
        raise MetaAPIError(resp.status_code, data)

    return data


class MetaClient:
    def __init__(
        self,
        *,
        page_id: str,
        ig_business_id: str,
        access_token: str,
        base_url: str = "https://graph.facebook.com/v21.0/",
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self._page_id = page_id
        self._ig_business_id = ig_business_id
        self._token = access_token
        self._base = base_url
        self._client = httpx.Client(timeout=15, transport=transport)

    @classmethod
    def from_config(cls, config: Config, transport: Optional[httpx.BaseTransport] = None) -> "MetaClient":
        return cls(
            page_id=config.meta_page_id,
            ig_business_id=config.meta_ig_business_id,
            access_token=config.meta_page_access_token,
            base_url=config.graph_api_base(),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MetaClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        params["access_token"] = self._token
        resp = self._client.get(f"{self._base}{path}", params=params)
        return _handle_response(resp)

    def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = dict(data or {})
        data["access_token"] = self._token
        resp = self._client.post(f"{self._base}{path}", data=data)
        return _handle_response(resp)

    # ---- reads ----

    def get_page_info(self) -> Dict[str, Any]:
        return self._get(self._page_id, params={"fields": "name,id"})

    def get_ig_account_info(self) -> Dict[str, Any]:
        return self._get(self._ig_business_id, params={"fields": "username,id"})

    def get_comment(self, comment_id: str) -> Dict[str, Any]:
        return self._get(
            comment_id,
            params={"fields": "id,message,from,created_time,parent,can_reply_privately"},
        )

    def get_conversation(self, conversation_id: str, limit: int = 20) -> Dict[str, Any]:
        return self._get(
            f"{conversation_id}/messages",
            params={"fields": "id,message,from,created_time", "limit": limit},
        )

    # ---- writes ----

    def reply_to_comment(self, comment_id: str, message: str) -> Dict[str, Any]:
        return self._post(f"{comment_id}/comments", data={"message": message})

    def hide_comment(self, comment_id: str, hide: bool = True) -> Dict[str, Any]:
        return self._post(comment_id, data={"is_hidden": "true" if hide else "false"})

    def send_dm(self, recipient_id: str, message: str) -> Dict[str, Any]:
        """Sends a DM via the Send API. Works for both Facebook Messenger and
        Instagram DMs — the Page's Graph API access token and the Send API
        endpoint are shared across both surfaces."""
        payload = {"recipient": {"id": recipient_id}, "message": {"text": message}}
        resp = self._client.post(
            f"{self._base}{self._page_id}/messages",
            params={"access_token": self._token},
            json=payload,
        )
        return _handle_response(resp)


def verify_webhook_signature(
    payload_body: bytes, signature_header: Optional[str], app_secret: str
) -> bool:
    """Verifies Meta's X-Hub-Signature-256 header against the raw request body.

    Uses hmac.compare_digest (constant-time) to avoid leaking timing info
    that could help an attacker forge webhook deliveries.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)
