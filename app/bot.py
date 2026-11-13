import logging

from telegram.ext import Application

from app.config import settings
from app.handlers import register_handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


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