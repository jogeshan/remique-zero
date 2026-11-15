
import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.webhook import router as webhook_router
from app.bot import create_telegram_application
from app.config import settings
from app.database import close_database, get_session_factory
from app.services.delivery_service import process_due_deliveries


logger = logging.getLogger(__name__)

DELIVERY_INTERVAL_SECONDS = 60


async def _delivery_loop(bot) -> None:
    """
    Deliver due reminders every minute.

    The first pass runs immediately at startup, which is the
    catch-up mechanism for reminders that came due while the
    service was asleep on Render: they are simply overdue jobs
    and are delivered by that first pass. A failed pass is
    logged and retried on the next tick; it never kills the loop.
    """
    while True:
        try:
            await process_due_deliveries(
                get_session_factory(),
                bot,
            )
        except Exception:
            logger.exception("Reminder delivery pass failed.")

        await asyncio.sleep(DELIVERY_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_app = None
    delivery_task = None
    initialized = False
    started = False

    app.state.telegram_application = None

    try:
        if settings.app_env.lower() == "production":
            webhook_url = (
                settings.telegram_webhook_url.strip()
                or (
                    os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
                    + "/telegram/webhook"
                )
            )

            if not webhook_url.startswith("https://"):
                raise RuntimeError(
                    "A valid HTTPS Telegram webhook URL is required."
                )

            if not settings.telegram_webhook_secret:
                raise RuntimeError(
                    "TELEGRAM_WEBHOOK_SECRET is missing."
                )

            telegram_app = create_telegram_application()

            await telegram_app.initialize()
            initialized = True

            await telegram_app.start()
            started = True

            app.state.telegram_application = telegram_app

            await telegram_app.bot.set_webhook(
                url=webhook_url,
                secret_token=settings.telegram_webhook_secret,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
            )

            logger.info(
                "Telegram webhook registered successfully: %s",
                webhook_url,
            )

            delivery_task = asyncio.create_task(
                _delivery_loop(telegram_app.bot)
            )

        yield

    finally:
        if delivery_task is not None:
            delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await delivery_task

        if telegram_app is not None:
            app.state.telegram_application = None

            if started:
                await telegram_app.stop()

            if initialized:
                await telegram_app.shutdown()

        await close_database()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="A private, invite-only AI Telegram assistant.",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(webhook_router)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "running",
        "docs": "/docs",
    }
