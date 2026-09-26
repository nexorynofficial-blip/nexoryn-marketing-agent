"""Standalone setup check.

Run this after filling in .env, before writing or running anything else:

    python scripts/verify_setup.py

It: (1) loads and validates every required env var, failing fast with a
clear message if any are missing; (2) initializes the local SQLite schema;
(3) calls the Meta Graph API to print your Page name and IG username, to
confirm META_PAGE_ACCESS_TOKEN / META_PAGE_ID / META_IG_BUSINESS_ID are
correct; (4) calls Slack's auth.test and posts a test message to the
approvals channel, to confirm SLACK_BOT_TOKEN and SLACK_APPROVALS_CHANNEL_ID
are correct and the bot is actually in that channel.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.config import load_config, mask_secret
from app.db import init_db


def check_config():
    print("== Config ==")
    config = load_config()
    print("  Loaded all required environment variables:")
    print(f"    META_APP_ID                = {config.meta_app_id}")
    print(f"    META_APP_SECRET            = {mask_secret(config.meta_app_secret)}")
    print(f"    META_PAGE_ID               = {config.meta_page_id}")
    print(f"    META_PAGE_ACCESS_TOKEN     = {mask_secret(config.meta_page_access_token)}")
    print(f"    META_IG_BUSINESS_ID        = {config.meta_ig_business_id}")
    print(f"    META_WEBHOOK_VERIFY_TOKEN  = {mask_secret(config.meta_webhook_verify_token)}")
    print(f"    SLACK_BOT_TOKEN            = {mask_secret(config.slack_bot_token)}")
    print(f"    SLACK_APP_TOKEN            = {mask_secret(config.slack_app_token)}")
    print(f"    SLACK_APPROVALS_CHANNEL_ID = {config.slack_approvals_channel_id}")
    print(f"    ANTHROPIC_API_KEY          = {mask_secret(config.anthropic_api_key)}")
    print(f"  Graph API version: {config.meta_graph_api_version}")
    print(f"  DB path: {config.db_path}")
    return config


def check_db(config):
    print("\n== Database ==")
    init_db(config.db_path)
    print(f"  Schema created/verified at {config.db_path}")


def check_meta(config):
    print("\n== Meta Graph API ==")
    base = config.graph_api_base()
    with httpx.Client(timeout=15) as client:
        try:
            resp = client.get(
                f"{base}{config.meta_page_id}",
                params={"fields": "name", "access_token": config.meta_page_access_token},
            )
            resp.raise_for_status()
            page_name = resp.json().get("name")
            print(f"  Page name: {page_name}")
        except httpx.HTTPStatusError as exc:
            print(f"  [FAILED] Could not fetch Page info: {exc.response.status_code} {exc.response.text}")
            raise

        try:
            resp = client.get(
                f"{base}{config.meta_ig_business_id}",
                params={"fields": "username", "access_token": config.meta_page_access_token},
            )
            resp.raise_for_status()
            ig_username = resp.json().get("username")
            print(f"  IG username: @{ig_username}")
        except httpx.HTTPStatusError as exc:
            print(f"  [FAILED] Could not fetch IG business account info: {exc.response.status_code} {exc.response.text}")
            raise


def check_slack(config):
    print("\n== Slack ==")
    client = WebClient(token=config.slack_bot_token)

    try:
        auth = client.auth_test()
        print(f"  Bot authenticated as: {auth['user']} (team: {auth['team']})")
    except SlackApiError as exc:
        print(f"  [FAILED] auth.test failed: {exc.response['error']}")
        raise

    try:
        result = client.chat_postMessage(
            channel=config.slack_approvals_channel_id,
            text="✅ Nexoryn Social Agent — verify_setup.py test message. Setup looks good so far.",
        )
        print(f"  Posted test message to channel {config.slack_approvals_channel_id} (ts={result['ts']})")
    except SlackApiError as exc:
        print(f"  [FAILED] Could not post to approvals channel: {exc.response['error']}")
        print("  (Make sure the bot has been invited to that channel with /invite @your-bot-name)")
        raise


def main():
    try:
        config = check_config()
        check_db(config)
        check_meta(config)
        check_slack(config)
    except Exception as exc:  # noqa: BLE001 - top-level script, surface any failure clearly
        print(f"\nSetup verification FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\nAll checks passed. Setup looks good.")


if __name__ == "__main__":
    main()
