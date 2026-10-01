# Nexoryn Agent CLI

A local, on-demand CLI tool that analyzes engagement on Nexoryn's own
Facebook Page — unanswered comments, low-engagement posts, and DM theme
patterns — and prints a summary with Claude-generated, actionable
recommendations. No server, no Slack, no webhooks: run it whenever you
want a status check, or loop it in the background (see below).

> **Note on project history:** this CLI is a ground-up refactor of an
> earlier version of this project that ran as a FastAPI webhook server
> with a Slack-based approval workflow for drafting and sending replies.
> That architecture is gone. If you're looking at old docs, issues, or
> commit history referencing Slack/webhooks/systemd/Caddy, that's why.

## What it does

- Pulls recent posts, comments, and DM conversations from your Facebook
  Page via the Graph API.
- Finds comments nobody from the Page has replied to yet.
- Finds posts with low engagement (reactions + comments below a threshold).
- Categorizes recent DMs into themes (shipping, product questions, order
  status, feedback, bugs, other) using Claude.
- If there's nothing to act on, says so plainly instead of inventing
  busywork. If there is, asks Claude for 3-5 specific, actionable steps.
- Caches Graph API responses locally for 30 minutes so repeated runs
  don't re-fetch the same data (use `--force` to bypass).
- Outputs either human-readable text or valid JSON (for a future
  dashboard to consume).

## Prerequisites

- Python 3.11+
- A Facebook Page access token (Graph API Explorer, or a System User token for something longer-lived)
- An Anthropic API key

## Setup (fresh clone)

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS/Linux
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run setup — it creates `.env` from the template if you don't have one
   yet, validates it, and initializes the local database:

   ```bash
   python scripts/setup.py
   ```

   If it created a fresh `.env`, open it and fill in the two variables
   (see `.env.example` for where to get each one), then re-run
   `python scripts/setup.py`.

4. Try it:

   ```bash
   python app/cli.py summary
   python app/cli.py summary --output json
   python app/cli.py status
   ```

## Project layout

```
.
├── app/
│   ├── cli.py                 # entry point: `summary` and `status` commands (Typer)
│   ├── config.py                # env var loading/validation (2 required vars)
│   ├── db.py                      # SQLite schema + row<->dataclass helpers (posts/comments/messages/summaries)
│   ├── models.py                    # Post/Comment/Message dataclasses; Summary/Recommendation/DMTheme (pydantic)
│   ├── meta_client.py                 # Graph API: get_posts_since / get_comments_on_posts / get_inbox_messages + file cache
│   ├── claude_client.py                 # Claude calls: categorize_dm_theme, generate_recommendations
│   ├── summary_generator.py               # the analysis engine -- unanswered/low-engagement/themes/recommendations
│   └── logging_config.py                    # structured JSON logging (stdout + file)
├── scripts/
│   └── setup.py                # one-time init: creates .env from template, validates it, creates the DB
└── tests/
```

## Commands

### `summary`

```bash
python app/cli.py summary [--hours 24] [--output pretty|json] [--force]
```

- `--hours`: how far back to look (default 24).
- `--output`: `pretty` (default, for a terminal) or `json` (for piping/scripting).
- `--force`: skip the 30-minute Graph API cache and fetch fresh data.

Fetches posts/comments/DMs, stores them locally, analyzes them, saves the
summary, and prints it. Exits non-zero with a plain-English error (never a
raw stack trace) if something's misconfigured or the Meta API rejects the
request.

### `status`

```bash
python app/cli.py status
```

Shows the most recent summary's status and action-item count, plus the
last 5 summaries' history.

## Running it continuously

This is a CLI, not a service — there's no built-in scheduler. To check
every couple of hours and keep a dashboard-ready file updated, run it in
a loop via `tmux` (or any equivalent — `screen`, a cron job, a systemd
timer):

```bash
tmux new-session -d -s nexoryn
tmux send-keys -t nexoryn "cd /path/to/this/project && source .venv/bin/activate && while true; do python app/cli.py summary --output json > last_summary.json; sleep 7200; done" Enter
```

That re-checks every 2 hours (7200s) and writes the latest JSON summary to
`last_summary.json` — the file a future dashboard integration will read.

## Data

All state lives in a single local SQLite file, `nexoryn_agent.db`
(gitignored, created by `scripts/setup.py` or automatically on first
`summary` run). `.cache/` holds the Graph API response cache (also
gitignored). No external database, queue, or server — this is a
single-tenant, local-first tool.

## Secrets and logging

Tokens are never logged or printed in full — only the first/last 4
characters, via `app.config.mask_secret`. Never paste real secrets into a
chat with an AI assistant; they only ever go into your local `.env`.
Logging is structured JSON (`app/logging_config.py`, built on
`structlog`), written to stdout and to `app.log` by default — every entry
carries a timestamp, level, logger name, and whatever context a call site
passed via `extra={...}`.
