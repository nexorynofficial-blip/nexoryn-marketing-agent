# Nexoryn Social Agent

An AI agent that monitors Nexoryn's own Instagram and Facebook Page for
comments and DMs, drafts replies using Claude, and sends **nothing** without
a human clicking Approve in Slack. It does not post or schedule content
(that stays in Meta Business Suite), and it does not manage any client
accounts — only Nexoryn's own.

## Current status

Built in phases; see the build plan for details. **Phase A (scaffold +
config)** is complete: folder structure, SQLite schema, env var loading, and
`scripts/verify_setup.py` all work. Nothing that talks to Claude, Slack
buttons, or Meta webhooks exists yet — that comes in later phases.

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
├── knowledge/agency_knowledge.md   # agency facts, tone rules, handoff messages (edit this yourself)
├── app/
│   ├── config.py        # env var loading/validation
│   ├── db.py             # SQLite schema + connections
│   ├── models.py          # data classes for messages/drafts/webhook dedupe
│   ├── meta_client.py      # Graph API calls (Phase B)
│   ├── claude_client.py     # Claude classify/draft/translate calls (Phase B)
│   ├── knowledge.py          # loads + cache-prepares the knowledge file (Phase B)
│   ├── slack_app.py            # Slack Bolt app: approvals UI (Phase C)
│   ├── webhooks.py               # Meta webhook receiver (Phase D)
│   ├── pipeline.py                 # core event -> draft -> Slack flow (Phase D)
│   ├── dedupe.py                     # echo detection + draft expiry jobs (Phase E)
│   └── main.py                        # FastAPI app entrypoint
├── scripts/
│   ├── verify_setup.py    # run this first
│   └── backfill.py         # pulls last 24h of unanswered messages (Phase E)
└── tests/
```

## Data

All state lives in a single local SQLite file, `nexoryn_agent.db` (gitignored,
created automatically). No external database or task queue is used — this is
a single-tenant, local-first app.

## Secrets and logging

Tokens are never logged or printed in full — only the first/last 4
characters, via `app.config.mask_secret`. Never paste real secrets into a
chat with an AI assistant; they only ever go into your local `.env`.
