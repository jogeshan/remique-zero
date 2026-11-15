# Remique Zero

Free multi-user AI-powered Telegram assistant. Invite-only: users register
with an invitation code, chat with Gemini, and keep a persistent
conversation history. Roles: `super_admin`, `admin`, `user`.

## Stack

- FastAPI + python-telegram-bot (webhook in production, polling locally)
- Google Gemini (`google-genai`)
- SQLAlchemy (async) + Alembic, Postgres (Neon)
- Deployed on Render via the `Procfile`

## Commands

- `/start` — register with an invite link, or open your account
- `/help` — show commands for your role
- `/newchat` — start a fresh conversation (the previous one is preserved)
- `/memories` — what the bot remembers about you
- `/remind`, `/reminders` — set and list reminders
- `/expense`, `/income`, `/summary`, `/debts` — money tracking
- `/tasks` — personal task list
- `/settings` — timezone and language
- `/apitoken` — get a token for the web dashboard (`/dashboard`)
- `/export` — download all of your data as a JSON file
- Admin: `/invite`, `/revoke`, `/users`, `/disable`, `/enable`
- Super admin only: `/promote`, `/demote`

Plain text messages in a private chat are sent to the AI. Daily limits:
30 requests/user/day by default, 300 for the super admin.

## Setup

1. Create a bot with BotFather and copy its token.
2. Create a Neon Postgres database and copy its connection string.
3. Get a Gemini API key from Google AI Studio.
4. Copy `.env.example` to `.env` and fill in the values. Set
   `BOOTSTRAP_ADMIN_TELEGRAM_ID` to your own Telegram user ID — the first
   `/start` from that account becomes the super admin.
5. Install and migrate:

   ```bash
   pip install -r requirements.txt
   alembic upgrade head
   ```

6. Run locally (polling):

   ```bash
   python -m app.bot
   ```

   Or run the web app (production uses the Telegram webhook; a valid
   HTTPS `TELEGRAM_WEBHOOK_URL` — or Render's `RENDER_EXTERNAL_URL` —
   and `TELEGRAM_WEBHOOK_SECRET` are required):

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

## Tests

```bash
pytest
```

Unit tests run anywhere. The integration test in
`tests/test_auth_integration.py` needs a real database and is skipped
automatically when `DATABASE_URL` is not configured.

## Features

Persistent per-user memory, reminders with a retrying delivery
worker (missed reminders are delivered on wake), expense/income
and debt tracking with daily check-ins and a morning overview,
personal task lists, multimodal input (photos, voice, documents),
conversation summarization for long chats, a read-only web
dashboard (`/dashboard` + `/api/v1/*`), and per-user data export
(`/export`).

## Production operations

### Required environment variables

Set these in the Render dashboard (Environment). `.env.example`
lists every supported variable; the ones below are required in
production:

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | Set to `production` (enables webhook mode) |
| `DATABASE_URL` | Neon Postgres connection string (pooled) |
| `DATABASE_URL_UNPOOLED` | Neon direct connection string, used by Alembic migrations |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Secret Telegram sends with each webhook call; the webhook rejects calls without it |
| `TELEGRAM_WEBHOOK_URL` | Public HTTPS webhook URL; optional on Render (falls back to `RENDER_EXTERNAL_URL`) |
| `GEMINI_API_KEY` | Google AI Studio key |
| `BOOTSTRAP_ADMIN_TELEGRAM_ID` | Telegram ID that becomes super admin on first `/start` |

Optional tuning: `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`,
`DEFAULT_TIMEZONE`, `MAX_AI_REQUESTS_PER_USER_PER_DAY`,
`SUPER_ADMIN_MAX_AI_REQUESTS_PER_DAY`, `MAX_MEDIA_SIZE_MB`,
`RATE_LIMIT_ENABLED`, `API_RATE_LIMIT_PER_MINUTE` (default 120),
`WEBHOOK_RATE_LIMIT_PER_MINUTE` (default 600).

### Deploy / start command

The Render start command (also in the `Procfile`) is:

```bash
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Migrations run on every deploy, before the app starts. If the
Render dashboard has a custom Start Command set, it overrides the
Procfile — make sure it matches the command above, otherwise new
tables are never created and features fail at runtime with
"couldn't record that"-style errors.

Health checks: `GET /health` is a cheap liveness probe.
`GET /health/deep` also checks database connectivity and reports
the database's Alembic revision versus the code's head revision —
`status: "degraded"` there means the database is unreachable or
behind on migrations.

### Keep the service awake (Render Free)

Render Free spins the service down after ~15 minutes without
HTTP traffic, and a sleeping service cannot deliver reminders on
time (missed ones are delivered on the next wake). To keep it
awake, ping the root URL every 5 minutes from any cron source —
for example a Hostinger or cPanel cron job:

```cron
*/5 * * * * curl -s -o /dev/null https://remique-zero.onrender.com/
```

Note the budget: always-awake uses roughly 720 of Render Free's
750 instance-hours per month, leaving little room for a second
always-on service.

### Migrations and the catch-up note

Normally `alembic upgrade head` in the start command keeps the
database current. If the production database ever falls behind
(e.g. the start command was overridden and tables are missing),
either fix the start command and redeploy, or run the pending
migrations' SQL once by hand in the Neon SQL Editor and set
`alembic_version` to the head revision afterwards. Check the
current state any time with:

```sql
SELECT version_num FROM alembic_version;
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' ORDER BY 1;
```

### Backups

All durable data lives in Neon Postgres (the app itself is
stateless). Neon keeps point-in-time history on its console, so a
restore is a branch/restore operation there — check the retention
window on your plan. For an independent copy, run a scheduled
logical backup from any machine with access:

```bash
pg_dump "$DATABASE_URL_UNPOOLED" -Fc -f remique-zero-$(date +%F).dump
```

Users can also export their own data at any time with `/export`.

### Rotating secrets

- **Bot token:** in Telegram, message BotFather → `/revoke` for
  the bot to issue a new token, then update `TELEGRAM_BOT_TOKEN`
  in Render and redeploy. The old token stops working
  immediately; the webhook is re-registered on startup.
- **Gemini key:** create a new key in Google AI Studio, update
  `GEMINI_API_KEY` in Render, redeploy, then delete the old key.
- **Dashboard tokens:** users rotate their own with `/apitoken`
  (the previous token is revoked instantly). Only SHA-256 hashes
  of tokens are stored.

## Security model

- **Backend-enforced permissions.** Roles (`super_admin`, `admin`,
  `user`) and per-user data scoping are enforced in the service
  layer on every query — never by AI instructions. AI tools act
  only as the Telegram user who sent the message, and every AI
  tool execution is written to the admin audit log.
- **Invite-only registration.** Invite codes are stored as hashes
  with use limits and expiry; there is no open sign-up.
- **Token hashing.** Dashboard API tokens are shown once and
  stored only as SHA-256 hashes, like passwords.
- **Webhook verification.** Telegram webhook calls must carry the
  configured secret token (constant-time comparison).
- **HTTP hardening.** Security headers on all responses
  (`X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, a Content-Security-Policy for the
  self-contained dashboard page), per-IP rate limits on
  `/api/v1/*` and the webhook, and a consistent
  `{"error": "..."}` shape for API errors.
- **Secrets stay secret.** Tokens and keys live only in
  environment variables; raw tokens, invite codes and export
  contents are never written to logs.
