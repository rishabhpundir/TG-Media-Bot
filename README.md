# Football Highlights Bot — TG / n8n / Stats Orchestrator

A private, owner-only Telegram bot that turns football fixtures into highlight
clips. It ingests match data hourly, lets the owner browse matches and find
highlights on YouTube, archive full videos to a private channel, and optionally
auto-clip highlights into short vertical videos via Opus Clip — all from chat.

This README explains the whole system end to end: the n8n data pipeline, the
PostgreSQL store, the Python Telegram bot, the local Telegram Bot API server,
the optional Opus auto-clipping, and the optional "remote yt-dlp" deployment
that offloads video downloading to a home PC.

---

## What it does (the pipeline)

```
football-data.org
      │  (hourly)
      ▼
   n8n workflow ──upsert──► PostgreSQL (matches, settings, jobs, …)
                                  ▲
                                  │ reads/writes
                                  ▼
                          Python Telegram bot  ◄──► owner (Telegram)
                          /     │        │     \
                  browse/    YouTube   archive   Opus Clip
                  filter     search    video     auto-clip
                            (OAuth)      │          │
                                         ▼          ▼
                              local Bot API     short vertical
                              server (2 GB)     clips delivered
                                         │       back to chat
                                         ▼
                              private archive channel
```

In one sentence: **football-data.org → n8n (hourly) → PostgreSQL → Telegram bot
(browse / search / archive) → optional Opus Clip (auto-clip) → clips delivered
back into Telegram.**

---

## Components

### 1. n8n — data ingestion (Node.js)
An n8n workflow ("Football Match Collector") runs hourly (Cron) and on demand
(webhook `/webhook/fetch-matches`). It calls football-data.org, flattens the
nested JSON into snake_case columns, and upserts into the `matches` table keyed
on `fixture_id`. n8n runs as a Docker container and is the **only Node.js piece**
of the system. The bot triggers the webhook for the `/refresh` command but never
re-implements ingestion.

### 2. PostgreSQL — the store
PostgreSQL 16 holds everything: match data, competition filters, cached YouTube
results, video metadata, Opus clip jobs/outputs, key/value settings, and the
remote-archive job queue. It is the single source of truth shared by n8n, the
bot, and (indirectly) the optional archive API.

### 3. The Telegram bot (Python)
The core application: a private, owner-locked bot built on
python-telegram-bot. It provides match browsing, league filtering, YouTube
search, video archiving, optional Opus auto-clipping, CSV export, a background
scheduler (JobQueue), a global error handler, and rotating file logs. Only the
configured owner can use it; everyone else is ignored.

### 4. Local Telegram Bot API server
Telegram's public Bot API caps **bot uploads at 50 MB**. Full-quality highlights
are 150–400 MB. So the bot talks to a **self-hosted Telegram Bot API server**
(compiled from tdlib/telegram-bot-api) running in `--local` mode on
`localhost:8081`, which raises the upload limit to 2 GB. The bot token is
migrated off the cloud onto this local server (one-time `logOut`), so only one
poller may ever run at a time.

### 5. Opus Clip — optional auto-clipping
When enabled, the bot can send a highlight video to the Opus Clip API, which
auto-detects the best moments and produces short vertical clips (for
TikTok/Reels/Shorts). This is **off by default**, gated behind a monthly credit
budget, with a cost preview before every spend and automatic disable at the cap.
Credits are charged by **source-video length**, not clip count.

### 6. Remote yt-dlp — optional archive offload to a home PC
YouTube blocks video downloads from datacenter IPs (the server) with
"Sign in to confirm you're not a bot", but allows them from residential IPs (a
home PC). To work around this, archiving can be **offloaded to the owner's PC**:

- A switch `USE_REMOTE_YTDLP=true/false` selects the path. **False** (default):
  the bot downloads and uploads on the server, exactly as before. **True**: the
  bot enqueues an `archive_jobs` row instead.
- A small **HTTP API** on the server (`archive_api.py`, token-authenticated)
  hands jobs to the PC and receives the finished file, which it uploads to the
  archive channel via the local Bot API server. PostgreSQL is never exposed.
- A standalone **PC agent** (`archive_agent.py`) polls the API, downloads with
  yt-dlp (residential IP, no Google account needed), uploads the file back to
  the server, and keeps a local copy in a `videos/` folder.

The local path always remains as a fallback: if the PC is off, flip
`USE_REMOTE_YTDLP=false` and archiving runs on the server again.

---

## Tech stack

**Non-Node.js (the bot and its system dependencies)**
- Python 3.12 (venv + pip)
- python-telegram-bot[ext] (the `[ext]` provides JobQueue), asyncpg, httpx,
  google-auth-oauthlib, google-api-python-client, apscheduler, python-dotenv,
  pydantic, pydantic-settings
- FastAPI + uvicorn + python-multipart (the optional archive API)
- PostgreSQL 16
- ffmpeg (system) — yt-dlp uses it to merge video + audio
- yt-dlp (system binary) — downloads YouTube videos
- telegram-bot-api (compiled from source) — the 2 GB local upload server

**Node.js**
- n8n (Docker) — the hourly match-data collector

---

## Repository layout

```
.
├── main.py                     # bot entrypoint (polling, logging, wiring)
├── archive_api.py              # OPTIONAL FastAPI service for remote yt-dlp
├── requirements.txt
├── .env.example
├── app/
│   ├── config.py               # typed settings from .env (fails fast)
│   ├── db.py                   # asyncpg pool + conn() context manager
│   ├── auth.py                 # owner_only decorator / is_owner
│   ├── formatting.py           # HTML-escape + match card formatting
│   ├── error_handler.py        # global error handler -> logs + pings owner
│   ├── tasks.py                # JobQueue jobs (poll/dispatch/budget/cleanup/archive)
│   ├── commands/               # /start /help /matches /today /live /upcoming
│   │   ├── basic.py            #   /leagues /refresh /search /opus /csv
│   │   ├── leagues.py
│   │   ├── refresh.py
│   │   ├── matches.py
│   │   ├── youtube_cmd.py      # /search, Find Highlights, Archive callback
│   │   ├── opus_cmd.py         # /opus on|off|status|cap|setbrand|listbrands|prompt
│   │   ├── opus_create.py      # auto-clip flow (cost preview, confirm/cancel)
│   │   └── csv_cmd.py          # /csv export
│   └── services/
│       ├── n8n.py              # webhook trigger for /refresh
│       ├── competitions.py     # league filters / eligibility
│       ├── matches.py          # match queries
│       ├── youtube_auth.py     # YouTube OAuth (token.json)
│       ├── youtube.py          # YouTube search + caching + video_meta
│       ├── archive.py          # yt-dlp download + channel upload (local path)
│       ├── archive_jobs.py     # OPTIONAL remote-archive queue helpers
│       ├── settings_store.py   # key/value settings + curation prefs
│       ├── budget.py           # Opus credit budget
│       └── opus.py             # Opus Clip API client
├── schema/
│   ├── schema.sql              # full DDL (disaster recovery / fresh build)
│   └── seed.sql                # settings + competition_filters config rows
├── n8n/                        # n8n workflow export + notes
├── pc_agent/                   # OPTIONAL: archive_agent.py for the home PC
└── docs/                       # guides (see "Documentation" below)
```

---

## Database (PostgreSQL `football_stats`)

- **matches** — fixtures from football-data.org (snake_case columns) plus
  `is_eligible`, `youtube_checked`, `highlight_found`, timestamps + auto-touch
  trigger. Populated by n8n.
- **competition_filters** — which competitions are enabled (drives eligibility).
- **youtube_results** — per-fixture YouTube search cache (24h TTL).
- **video_meta** — `video_id` → title/channel/duration, so any searched video
  (including freeform `/search`) has a title for archive captions.
- **clip_jobs / clip_outputs** — Opus job lifecycle and resulting clips.
- **settings** — key/value config (`opus_enabled`, `opus_monthly_cap`,
  `opus_topic_keywords`, `opus_brand_template_id`, …).
- **archive_jobs** — OPTIONAL remote-archive queue (status: queued → claimed →
  uploading → done/failed; `channel_message_id`, `owner_notified`, …).

Full DDL is in `schema/schema.sql`; config rows in `schema/seed.sql`.
(Re-dump the schema with `pg_dump --schema-only --no-owner --no-acl` so it loads
cleanly on a fresh database.)

---

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Wake / list commands |
| `/matches`, `/today`, `/live`, `/upcoming` | Browse matches (paginated cards) |
| `/leagues` | Toggle which competitions are included |
| `/refresh` | Trigger n8n ingestion immediately |
| `/search <query>` | Freeform YouTube search |
| `/opus status` | Auto-clip on/off + credit usage |
| `/opus on` / `/opus off` | Enable / disable auto-clipping |
| `/opus cap <N>` | Set monthly credit limit |
| `/opus prompt <keywords>` | Steer which moments Opus prioritises |
| `/opus listbrands` / `/opus setbrand <id>` | Manage Opus brand templates |
| `/csv [all]` | Export matches as a downloadable CSV |

Buttons under each video result: **Archive** (save full video to the channel),
**Open in Opus** (manual edit), **Auto-clip** (generate short clips).

---

## Configuration (`.env`)

Copy `.env.example` to `.env` and fill in. Groups:

- **Project** — `FOOTBALL_BOT_HOME` (absolute path), `LOG_LEVEL`,
  `DISPLAY_TIMEZONE`
- **Postgres** — `POSTGRES_HOST` (127.0.0.1 in production), `POSTGRES_PORT`,
  `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASS`
- **Telegram** — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`,
  `TELEGRAM_ARCHIVE_CHANNEL_ID` (the bot must be an **admin** of this channel)
- **Local Bot API** — `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
  `TELEGRAM_LOCAL_API=true`, `TELEGRAM_LOCAL_API_URL=http://localhost:8081`
- **YouTube** — `YOUTUBE_CREDENTIALS_PATH`, `YOUTUBE_TOKEN_PATH`
- **Opus** (optional) — `OPUS_API_KEY`, `OPUS_ORG_ID`
- **n8n** — `N8N_BASE_URL`, `N8N_WEBHOOK_PATH`
- **Remote yt-dlp** (optional) — `USE_REMOTE_YTDLP` (false default),
  `ARCHIVE_API_TOKEN`, `ARCHIVE_API_PORT`
- **Misc** — `CLIENT_EMAIL`

See `requirements.txt` for Python packages and `.env.example` for the full
annotated list. Secrets live only in `.env` (never in code).

---

## Setup & deployment (summary)

Detailed step-by-step instructions are in `docs/` (see below). In brief, on a
fresh Ubuntu server:

1. System packages: Python 3.12, ffmpeg, yt-dlp (system binary).
2. PostgreSQL 16 — if fresh, load `schema/schema.sql` then `schema/seed.sql`.
3. Compile the local Telegram Bot API server from source (needs 4 GB RAM or a
   swap file + `-j1` build on small servers).
4. n8n in Docker (hourly collector) — import the workflow from `n8n/`.
5. Python venv + `pip install -r requirements.txt`.
6. Create `.env`; authorise YouTube (generate `token.json`).
7. Run the bot and Bot API server as systemd services (`football-bot.service`,
   `telegram-bot-api.service`); n8n is Docker `--restart unless-stopped`.
8. Nightly `pg_dump` backup via cron; lock the firewall (only SSH + the archive
   API port, if used, are public).

### Enabling remote yt-dlp (optional)
1. `archive_jobs` table is in the schema (additive).
2. Install `fastapi uvicorn[standard] python-multipart`; run `archive_api.py` as
   `archive-api.service`; open `ARCHIVE_API_PORT` (token-protected).
3. Set `USE_REMOTE_YTDLP=true` and restart the bot.
4. On the owner's PC: install Python + yt-dlp + ffmpeg, configure and run
   `archive_agent.py` (ideally as a background Windows service via NSSM).
5. Flip back to `false` at any time to fall back to server-side archiving.

---

## Operations

- **Services**: `systemctl status football-bot.service telegram-bot-api.service`
  (and `archive-api.service` if remote archiving is on). n8n: `docker ps`.
- **Logs**: app log at `logs/bot.log` (rotating). Or the journal:
  `journalctl -u football-bot.service -f`. Archive API:
  `journalctl -u archive-api.service -f`. n8n: `docker logs -f n8n`.
- **Backups**: nightly `pg_dump football_stats | gzip` to `/opt/backups`,
  7-day retention (cron).
- **Single-poller rule**: only ONE bot instance may poll the token at a time
  (running two causes a 409 Conflict). Never run dev and prod simultaneously.

---

## Documentation

Companion guides (in `docs/`):

- **DEPLOYMENT.txt** — full step-by-step server deployment runbook.
- **Bot-User-Guide.md** — client-facing guide to every command.
- **TOKEN-SETUP-GUIDE.md** — how to generate the YouTube `token.json`.
- **PC-AGENT-SETUP-GUIDE.md** — installing/running the home-PC archive agent.
- **RUN-AGENT-IN-BACKGROUND.md** — running the agent as a boot-proof Windows
  service (NSSM) and preventing sleep.

---

## Notes & gotchas

- The bot uses **HTML parse mode** — any literal `<`/`>` in messages must be
  escaped (`&lt;`/`&gt;`).
- Opus is **optional and off by default**; credits charge by source length, not
  clip count. To save credits, clip shorter source videos.
- The archive feature is the part most exposed to YouTube's bot-detection on a
  server IP; the remote-yt-dlp option (home PC) is the robust fix.
- YouTube OAuth: the **account that approves consent** is the identity the bot
  searches as. For long-running use, publish the OAuth consent screen (testing
  mode tokens expire after 7 days).
- Changing `.env` requires restarting the relevant service to take effect.


