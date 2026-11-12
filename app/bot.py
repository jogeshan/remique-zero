
import logging

from telegram import Update
from telegram.ext import Application

from app.config import settings
from app.handlers import register_handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from the environment."
        )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    register_handlers(application)

    logger.info("Starting Remique Zero Telegram bot...")

    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
