import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

from app.config import settings
from app.database import get_session_factory
from app.services import api_token_service, memory_service

logger = logging.getLogger(__name__)


def _dashboard_url() -> str:
    """
    Best-effort public dashboard URL.

    Derived from the configured webhook URL (or Render's external
    URL) so the link works in production; falls back to the bare
    path when no public base is known.
    """
    base = settings.telegram_webhook_url.strip().rstrip("/")

    if base.endswith("/telegram/webhook"):
        base = base[: -len("/telegram/webhook")]

    if not base:
        base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

    return f"{base}/dashboard" if base else "/dashboard"


async def apitoken_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    /apitoken — issue (or rotate) the user's web dashboard token.

    The raw token is sent exactly once, in this reply. Only its
    SHA-256 hash is stored, so it cannot be shown again; running
    the command again revokes the previous token and issues a new
    one.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "Please use /apitoken in a private conversation — "
            "the token is a secret."
        )
        return

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await memory_service.get_active_user_by_telegram_id(
                session,
                user.id,
            )

            if account is None:
                await message.reply_text(
                    "Your Remique Zero account is not active. "
                    "Please contact the administrator."
                )
                return

            _, raw_token = await api_token_service.create_token(
                session,
                account.id,
            )
    except Exception:
        logger.exception("Failed to create an API token.")
        await message.reply_text(
            "I couldn't create your dashboard token. "
            "Please try again."
        )
        return

    await message.reply_text(
        "🔑 Your dashboard token (shown only once — save it now):\n\n"
        f"{raw_token}\n\n"
        f"Dashboard: {_dashboard_url()}\n\n"
        "Paste the token into the dashboard to see your money, "
        "reminders, tasks and memories. Anyone with this token can "
        "view your data, so keep it private. Run /apitoken again "
        "to replace it — the old one stops working immediately."
    )
