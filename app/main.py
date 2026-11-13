
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.webhook import router as webhook_router
from app.bot import create_telegram_application
from app.config import settings
from app.database import close_database


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_app = None
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

        yield

    finally:
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
