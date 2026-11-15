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

## Not yet implemented

Reminders and scheduled delivery (the database schema for reminders,
delivery jobs and attempts already exists), voice messages, and image
interpretation.
