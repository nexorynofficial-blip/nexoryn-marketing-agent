# Nexoryn Social Agent

An AI agent that monitors Nexoryn's own Instagram and Facebook Page for
comments and DMs, drafts replies using Claude, and sends **nothing** without
a human clicking Approve in Slack. It does not post or schedule content
(that stays in Meta Business Suite), and it does not manage any client
accounts — only Nexoryn's own.

## Current status

Built in phases. **Phases A–D are complete**: config/DB scaffold, the Meta
and Claude clients, the Slack approvals app (post/Approve/Edit/Reject,
Hide/Keep for spam), and the webhook receiver + full pipeline wiring them
together. What's still missing (Phase E): backfilling the last 24h on
startup, echo detection (cancel a draft if a human already replied via
Meta Business Suite), and draft expiry/nudge background jobs.

## Prerequisites

- Python 3.11+
- A Meta App with a connected Facebook Page and linked Instagram professional
  account, with a long-lived Page access token
- A Slack app (Socket Mode enabled) installed to your workspace, invited into
  the approvals channel
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

3. Copy the env template and fill in real values:

   ```bash
   cp .env.example .env
   ```

   Open `.env` and fill in every variable — `.env.example` has a comment on
   each one explaining what it is and roughly where to find it. **Never
   commit `.env`** (it's already in `.gitignore`).

4. Replace the placeholder content in `knowledge/agency_knowledge.md` with
   real agency knowledge, tone-of-voice rules, and the three handoff message
   translations (English / Urdu script / Roman Urdu). This file is what the
   Claude drafting prompt is grounded in.

5. Verify everything is wired up correctly:

   ```bash
   python scripts/verify_setup.py
   ```

   This loads your config, creates the local SQLite schema
   (`nexoryn_agent.db`), fetches your Page name and IG username from the
   Graph API, and posts a test message to your Slack approvals channel. If
   anything is misconfigured, it fails with a specific error telling you
   what's wrong.

## Project layout

```
.
├── knowledge/agency_knowledge.md   # agency facts, tone rules, handoff messages, fixed pricing line (edit this yourself)
├── app/
│   ├── config.py        # env var loading/validation
│   ├── db.py             # SQLite schema, migrations, connections, row<->dataclass helpers
│   ├── models.py          # data classes + enums for messages/drafts/webhook dedupe
│   ├── meta_client.py      # Graph API calls + webhook signature verification
│   ├── claude_client.py     # Claude classify/draft/translate calls
│   ├── knowledge.py          # loads + cache-prepares the knowledge file, extracts fixed strings
│   ├── slack_app.py            # Slack Bolt app: post draft/spam-check/alert-only, button + modal handlers
│   ├── webhooks.py               # Meta webhook receiver: verify challenge, signature check, payload parsing, dedup
│   ├── pipeline.py                 # core event -> classify -> draft/handoff/spam routing -> Slack
│   ├── dedupe.py                     # webhook-event dedup (echo detection + expiry jobs: Phase E)
│   └── main.py                        # FastAPI app entrypoint; starts Slack Socket Mode alongside it
├── scripts/
│   ├── verify_setup.py         # run this first
│   ├── try_pipeline_sample.py    # classify+draft a few sample messages via the real API, no Slack/webhooks
│   ├── try_slack_draft.py          # posts fake drafts and runs Slack Socket Mode so you can click buttons live
│   └── backfill.py                   # pulls last 24h of unanswered messages (Phase E)
└── tests/
```

## Running the full app locally

```bash
uvicorn app.main:app --reload --port 8000
```

This starts the FastAPI webhook server on port 8000 **and** connects the
Slack app in Socket Mode in the background, so both webhook delivery and
Slack button clicks work from one process. Visit `http://localhost:8000/health`
to confirm it's up.

## Testing webhooks locally with cloudflared

Meta needs to reach your webhook endpoint over the public internet, so for
local development you need a temporary public URL pointed at your machine.
(Slack doesn't need this — Socket Mode is outbound-only.)

1. Install `cloudflared` (no account needed for a quick tunnel):
   - **Windows**: download the `cloudflared-windows-amd64.exe` from
     [Cloudflare's releases page](https://github.com/cloudflare/cloudflared/releases/latest),
     or `winget install --id Cloudflare.cloudflared`.
   - **macOS**: `brew install cloudflared`.
2. With `uvicorn app.main:app --port 8000` running in one terminal, run in another:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
3. Copy the generated `https://<random-words>.trycloudflare.com` URL from the
   terminal output.
4. In your Meta App dashboard → Webhooks, set the Callback URL to
   `https://<random-words>.trycloudflare.com/webhook` and the Verify Token to
   the same value as your `.env`'s `META_WEBHOOK_VERIFY_TOKEN`, then click
   Verify and Save. Meta will hit the GET `/webhook` endpoint with a
   challenge; a successful verify confirms the token matches on both ends.
5. Subscribe to the fields you need (Page/Instagram webhook product):
   `comments` (and `feed` if you also want Facebook Page comments) for new
   comments, and `messages` for new DMs.
6. Leave the tunnel and `uvicorn` running, then send a real test comment or
   DM to your Page/IG account and watch it show up in the Slack approvals
   channel.

Note: the cloudflared URL changes every time you restart the tunnel, so
you'll need to re-paste it into the Meta dashboard each time you restart it
during development.

## Data

All state lives in a single local SQLite file, `nexoryn_agent.db` (gitignored,
created automatically). No external database or task queue is used — this is
a single-tenant, local-first app.

## Secrets and logging

Tokens are never logged or printed in full — only the first/last 4
characters, via `app.config.mask_secret`. Never paste real secrets into a
chat with an AI assistant; they only ever go into your local `.env`.
