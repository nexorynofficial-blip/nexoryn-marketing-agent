# Nexoryn Social Agent

An AI agent that monitors Nexoryn's own Instagram and Facebook Page for
comments and DMs, drafts replies using Claude, and sends **nothing** without
a human clicking Approve in Slack. It does not post or schedule content
(that stays in Meta Business Suite), and it does not manage any client
accounts — only Nexoryn's own.

## Current status

Built in phases. **Phases A–F are complete**: config/DB scaffold, the Meta
and Claude clients, the Slack approvals app, the webhook receiver + full
pipeline, startup backfill, echo detection, draft expiry/nudge, structured
JSON logging, and Oracle Cloud VM deployment tooling (systemd + Caddy).
136/136 tests pass, all against mocked Meta/Slack/Anthropic responses.

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
│   ├── webhooks.py               # Meta webhook receiver: verify challenge, signature check, payload parsing, dedup, echo dispatch
│   ├── pipeline.py                 # core event -> classify -> draft/handoff/spam routing -> Slack
│   ├── dedupe.py                     # webhook-event dedup, echo detection, draft expiry/nudge
│   ├── logging_config.py               # structured JSON logging (stdout + optional file sink)
│   └── main.py                          # FastAPI entrypoint; startup backfill, Slack Socket Mode, APScheduler jobs
├── scripts/
│   ├── verify_setup.py         # run this first
│   ├── try_pipeline_sample.py    # classify+draft a few sample messages via the real API, no Slack/webhooks
│   ├── try_slack_draft.py          # posts fake drafts and runs Slack Socket Mode so you can click buttons live
│   ├── backfill.py                   # pulls unanswered comments/DMs from the last N hours; also runnable standalone
│   ├── setup_vm.sh                     # runs ON the VM: installs deps, systemd + Caddy + logrotate config
│   └── deploy_to_vm.sh                   # runs on YOUR laptop: copies code/.env to the VM, runs setup, starts the service
├── systemd/nexoryn-agent.service   # systemd unit -> /etc/systemd/system/ on the VM
├── caddy/Caddyfile                 # Caddy reverse-proxy + auto-HTTPS config -> /etc/caddy/ on the VM
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

On startup, before Slack connects, it runs a one-time backfill (last 24h of
unanswered comments/DMs) and then schedules two background jobs every 10
minutes: a fallback comment-echo poll (see below) and the draft
expiry/nudge check for DMs approaching Meta's 24h reply window.

To run just the backfill without starting the server:

```bash
python scripts/backfill.py               # last 24h, up to 100 messages
python scripts/backfill.py --hours 48 --limit 50
```

### Echo detection

If a teammate replies to a comment or DM via Meta Business Suite before a
Slack draft is approved, the pending draft is cancelled automatically (its
Slack card updates to "🔁 Already handled", buttons removed) rather than
risking a duplicate or contradictory reply. This is mostly event-driven —
DMs use the messaging webhook's `is_echo` field, comments use the new-
comment webhook's `parent` field — so it costs no extra Graph API calls in
the common case. The 10-minute comment-echo poll only exists as a fallback
safety net, in case a reply's webhook event ever lacks a usable parent id.

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

## Deploying to Oracle Cloud (production)

This runs the agent 24/7 on a real domain instead of a local machine +
cloudflared tunnel. **None of this has been executed or verified against a
real VM** — the scripts below were written carefully against standard
practice (Caddy's own defaults, systemd conventions, Ubuntu 24.04's
package set) but this development session had no VM/SSH/DNS access to
actually run them. Treat the first deploy as a real test, not a
rubber-stamped step.

### What you need before starting

- An Oracle Cloud "Always Free" VM (Ubuntu 24.04 LTS) already running, reachable via SSH.
- `agent.nexoryn.tech`'s DNS A record pointed at the VM's public IP (Caddy
  can't get a Let's Encrypt certificate until this resolves correctly).
- Your SSH private key for that VM.
- A filled-in `.env` (same 10+ variables as local dev, see `.env.example`) —
  set `DB_PATH=/opt/nexoryn-agent/nexoryn_agent.db` and
  `LOG_FILE_PATH=/var/log/nexoryn-agent/agent.log` in it for this environment.
- Oracle Cloud's security list / network security group must allow inbound
  TCP 80 and 443 (needed for Let's Encrypt's HTTP challenge and for HTTPS
  traffic) — this is an Oracle Cloud console setting, not something
  `setup_vm.sh` can configure from inside the VM.

### Deploy

From your laptop, in this project's root:

```bash
bash scripts/deploy_to_vm.sh <vm_ip_or_hostname> /path/to/ssh/key /path/to/.env
```

This will:
1. Check SSH connectivity.
2. Copy the project to `/opt/nexoryn-agent` on the VM (via rsync if
   available, otherwise a tarball over scp) — excluding `.venv`, `.git`,
   `__pycache__`, and your local `nexoryn_agent.db`.
3. **Ask you to confirm** before uploading `.env` (it contains real secrets).
4. Run `scripts/setup_vm.sh` on the VM via `sudo` — installs Python,
   Caddy, creates the `nexoryn` system user, sets up the venv, installs
   the systemd unit and Caddy config, sets up logrotate.
5. Start the service and print `systemctl status`.

Pass `--with-db` as a 4th argument if you want to upload your local
`nexoryn_agent.db` too (asks for separate confirmation) — otherwise the VM
starts with a fresh, empty database and `init_db()` creates the schema on
first run.

### Verifying it worked

```bash
ssh -i /path/to/ssh/key ubuntu@agent.nexoryn.tech "systemctl status nexoryn-agent --no-pager"
curl https://agent.nexoryn.tech/health          # expect {"status":"ok"} with a valid cert
ssh -i /path/to/ssh/key ubuntu@agent.nexoryn.tech "sudo journalctl -u nexoryn-agent -n 50 --no-pager"
```

Then re-point Meta's webhook Callback URL from your cloudflared URL to
`https://agent.nexoryn.tech/webhook` (same Verify Token as before), send a
real test comment or DM, and confirm it reaches Slack the same way it did
locally.

### Day-to-day monitoring

```bash
ssh -i /path/to/ssh/key ubuntu@agent.nexoryn.tech "systemctl status nexoryn-agent"
ssh -i /path/to/ssh/key ubuntu@agent.nexoryn.tech "journalctl -u nexoryn-agent -f"    # live tail
curl https://agent.nexoryn.tech/health
```

Logs are structured JSON (see `app/logging_config.py`) written to both
stdout (captured by journald automatically) and `/var/log/nexoryn-agent/agent.log`
(rotated daily by logrotate, 14 days kept, compressed). `/health` only
confirms FastAPI itself is up — it doesn't check Slack/Meta connectivity,
by design, so it stays useful as a liveness probe even if an external
service is degraded.

If the process crashes, systemd restarts it automatically
(`Restart=on-failure`). Re-running `deploy_to_vm.sh` (or just
`setup_vm.sh` on the VM) is safe — every step checks whether it's already
done first.

## Data

All state lives in a single local SQLite file, `nexoryn_agent.db` (gitignored,
created automatically). No external database or task queue is used — this is
a single-tenant, local-first app.

## Secrets and logging

Tokens are never logged or printed in full — only the first/last 4
characters, via `app.config.mask_secret`. Never paste real secrets into a
chat with an AI assistant; they only ever go into your local `.env`, or
get uploaded to the VM via `deploy_to_vm.sh`'s explicit confirmation
prompt. Logging is structured JSON (`app/logging_config.py`, built on
`structlog`) — every entry carries a timestamp, level, logger name, and
whatever context a call site passed via `extra={...}` (e.g. `message_id`,
`draft_id`); nothing beyond that is ever added automatically, so a log
call is only as safe as what you choose to pass it.
