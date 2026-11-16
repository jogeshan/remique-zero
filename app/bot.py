import logging

from telegram import BotCommand
from telegram.ext import Application

from app.config import settings
from app.handlers import register_handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# The user-facing command menu shown by Telegram clients. Admin
# commands are deliberately not listed here; they stay available
# by typing them (see /help for administrators).
USER_BOT_COMMANDS = [
    BotCommand("start", "🚀 Register or open your account"),
    BotCommand("help", "❓ Show all commands"),
    BotCommand("newchat", "💬 Start a new conversation"),
    BotCommand("memories", "🧠 What I remember about you"),
    BotCommand("remind", "⏰ Set a reminder"),
    BotCommand("reminders", "📋 List your reminders"),
    BotCommand("expense", "💸 Quick-add an expense"),
    BotCommand("income", "💰 Quick-add income"),
    BotCommand("summary", "📊 Money overview"),
    BotCommand("debts", "🤝 Your open debts and loans"),
    BotCommand("tasks", "✅ Your personal task list"),
    BotCommand("settings", "⚙️ Timezone and language"),
    BotCommand("apitoken", "🔑 Get your web dashboard token"),
    BotCommand("export", "📥 Download your data as a JSON file"),
]


async def setup_bot_commands(application: Application) -> None:
    """
    Register the bot command menu with Telegram (post_init hook).

    A failure here must never block startup, so it is logged and
    swallowed.
    """
    try:
        await application.bot.set_my_commands(USER_BOT_COMMANDS)
        logger.info("Bot command menu registered.")
    except Exception:
        logger.warning(
            "Failed to register the bot command menu.",
            exc_info=True,
        )


async def telegram_error_handler(update, context):
    error = context.error

    if error:
        logger.error(
            "Telegram handler error",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )


def create_telegram_application() -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    register_handlers(application)
    application.post_init = setup_bot_commands
    application.add_error_handler(telegram_error_handler)

    return application


def main() -> None:
    application = create_telegram_application()

    logger.info("Starting Remique Zero in polling mode...")

    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()