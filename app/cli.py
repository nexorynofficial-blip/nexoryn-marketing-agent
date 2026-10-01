"""Nexoryn Agent CLI: on-demand Facebook Page engagement analysis.

    python app/cli.py summary [--hours 24] [--output pretty|json] [--force]
    python app/cli.py status
"""
from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path
from typing import List

# Windows consoles often default to cp1252, which can't encode the
# emoji used in this CLI's output.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Allows `python app/cli.py ...` to find the `app` package -- without this,
# Python only puts app/'s own directory on sys.path, not its parent.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import typer

from app.claude_client import ClaudeClient
from app.config import load_config
from app.db import (
    get_connection,
    get_last_summary,
    get_recent_summaries,
    init_db,
    store_comments,
    store_messages,
    store_posts,
    store_summary,
    update_message_theme,
)
from app.logging_config import configure_logging
from app.meta_client import MetaAPIError, MetaClient
from app.models import Summary, UnansweredComment
from app.summary_generator import SummaryGenerator

app = typer.Typer(add_completion=False, help="Nexoryn Agent: Facebook Page engagement analysis.")
logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _fail(message: str) -> "typer.Exit":
    """Prints a user-friendly error (no stack trace) and signals a non-zero exit."""
    typer.secho(f"✖ {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


@app.command()
def summary(
    hours: int = typer.Option(24, help="How many hours back to look."),
    output: str = typer.Option("pretty", help="Output format: 'pretty' or 'json'."),
    force: bool = typer.Option(False, help="Bypass the cache and fetch fresh data from Meta."),
) -> None:
    """Fetch recent Facebook activity, analyze it, and print a summary."""
    if output not in ("pretty", "json"):
        raise _fail("--output must be 'pretty' or 'json'")

    try:
        config = load_config()
    except RuntimeError as exc:
        raise _fail(str(exc))

    configure_logging(level=config.log_level, log_file_path=config.log_file_path or "app.log")

    if output == "pretty":
        typer.echo(f"📱 Nexoryn Social Agent | Analyzing {hours}h of Facebook activity...\n")

    try:
        init_db(config.db_path)
        meta = MetaClient.from_config(config)
        claude = ClaudeClient(api_key=config.anthropic_api_key)

        posts = meta.get_posts_since(hours, force=force)
        comments = meta.get_comments_on_posts(posts, force=force)
        messages = meta.get_inbox_messages(hours, force=force)

        now = _now_iso()
        conn = get_connection(config.db_path)
        try:
            store_posts(conn, posts, now)
            store_comments(conn, comments, now)
            store_messages(conn, messages, now)

            generator = SummaryGenerator(
                claude, on_theme_categorized=lambda mid, theme: update_message_theme(conn, mid, theme)
            )
            result = generator.analyze(posts, comments, messages)
            store_summary(conn, result, now)
        finally:
            conn.close()
    except MetaAPIError as exc:
        raise _fail(f"Meta API error: {exc}")
    except Exception as exc:  # noqa: BLE001 - last resort, never show a raw traceback to the user
        logger.exception("summary_command_failed")
        raise _fail(f"Something went wrong: {exc}")

    if output == "json":
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_pretty(result)


@app.command()
def status() -> None:
    """Show the last summary's status and recent history."""
    try:
        config = load_config()
    except RuntimeError as exc:
        raise _fail(str(exc))

    configure_logging(level=config.log_level, log_file_path=config.log_file_path or "app.log")
    init_db(config.db_path)

    conn = get_connection(config.db_path)
    try:
        last = get_last_summary(conn)
        if last is None:
            typer.echo("No summary yet -- run `python app/cli.py summary` first.")
            return

        recent = get_recent_summaries(conn, limit=5)
    finally:
        conn.close()

    status_label = "Healthy" if last.status == "healthy" else "Action needed"
    typer.echo("✅ Nexoryn Agent Status\n")
    typer.echo(f"Last summary: {_format_display_time(last.timestamp)} ({_time_ago(last.timestamp)})")
    typer.echo(f"Status: {status_label}")
    typer.echo(f"Action items: {last.total_action_items}\n")

    typer.echo("Last 5 summaries:")
    for entry in recent:
        label = "items" if entry.total_action_items != 1 else "item"
        typer.echo(f"  {_format_display_time(entry.timestamp)} — {entry.total_action_items} {label}")


def _print_pretty(result: Summary) -> None:
    if result.status == "healthy":
        typer.echo(f"✅ {result.message}")
        typer.echo("   - All comments answered")
        typer.echo("   - Posts performing well")
        typer.echo("   - No unusual DM patterns\n")
        typer.echo(f"Last checked: {_format_display_time(result.timestamp)}")
        return

    typer.echo(f"📊 FACEBOOK PAGE SUMMARY ({_format_display_time(result.timestamp)})\n")

    if result.unanswered_comments:
        typer.echo(f"🔴 UNANSWERED COMMENTS ({len(result.unanswered_comments)})")
        for post_title, comments in _group_by_post(result.unanswered_comments):
            time_label = comments[0].time_since
            typer.echo(f'   Post: "{post_title}" ({time_label})')
            for comment in comments:
                typer.echo(f'   └ "{comment.comment_text}" — {comment.author}')
        typer.echo("")

    if result.low_engagement_posts:
        typer.echo(f"📉 LOW-ENGAGEMENT POSTS ({len(result.low_engagement_posts)})")
        for post in result.low_engagement_posts:
            typer.echo(f'   "{post.title}" ({post.posted_ago})')
            typer.echo(f"   └ {post.reactions} reactions, {post.comments} comments")
        typer.echo("")

    if result.dm_themes:
        typer.echo("💬 COMMON DM THEMES")
        for name, pct in result.dm_themes.items():
            typer.echo(f"   {name}: {pct}%")
        typer.echo("")

    if result.recommendations:
        typer.echo("🎯 TODAY'S RECOMMENDATIONS")
        for i, rec in enumerate(result.recommendations, start=1):
            typer.echo(f"   {i}. {rec}")
        typer.echo("")

    typer.echo(f"Last checked: {_format_display_time(result.timestamp)}")
    typer.echo("Next check in: 2 hours (or run --force to check now)")


def _group_by_post(comments: List[UnansweredComment]):
    seen_order: List[str] = []
    grouped: dict = {}
    for comment in comments:
        if comment.post_title not in grouped:
            seen_order.append(comment.post_title)
            grouped[comment.post_title] = []
        grouped[comment.post_title].append(comment)
    return [(title, grouped[title]) for title in seen_order]


def _format_display_time(iso_timestamp: str) -> str:
    """Cross-platform "Oct 1, 2026 5:45 PM" formatting -- avoids %-d/%#d,
    which only exist on one platform each."""
    try:
        parsed = dt.datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    date_part = parsed.strftime("%b %d, %Y").replace(" 0", " ")
    time_part = parsed.strftime("%I:%M %p")
    time_part = time_part.lstrip("0") or time_part
    return f"{date_part} {time_part}"


def _time_ago(iso_timestamp: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return "unknown"
    seconds = (dt.datetime.now() - parsed).total_seconds()
    if seconds < 60:
        return f"{int(seconds)} seconds ago"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} hours ago"
    return f"{int(hours / 24)} days ago"


if __name__ == "__main__":
    app()
