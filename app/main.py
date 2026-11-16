
import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.chat import chat_router
from app.api.dashboard import api_router as dashboard_api_router
from app.api.dashboard import page_router as dashboard_page_router
from app.api.health import router as health_router
from app.api.webhook import router as webhook_router
from app.bot import create_telegram_application
from app.config import settings
from app.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.database import close_database, get_session_factory
from app.services.delivery_service import process_due_deliveries


logger = logging.getLogger(__name__)

DELIVERY_INTERVAL_SECONDS = 10
DELIVERY_QUIET_STALENESS_SECONDS = 1200


async def _delivery_loop(bot) -> None:
    """
    Deliver due reminders, polling smartly.

    The first pass runs immediately at startup, which is the
    catch-up mechanism for reminders that came due while the
    service was asleep on Render: they are simply overdue jobs
    and are delivered by that first pass. A failed pass is
    logged and retried on the next tick; it never kills the loop.

    Between passes the loop remembers the earliest pending job
    (the next-due cache in delivery_service) and sleeps without
    touching the database while nothing is due, so an idle bot
    does not keep the Neon compute awake around the clock. A
    pass still runs at least every
    DELIVERY_QUIET_STALENESS_SECONDS while quiet (20 minutes —
    long enough for Neon to suspend its compute between passes)
    as a safety net.
    """
    from datetime import datetime, timezone

    from app.services import delivery_service

    session_factory = get_session_factory()
    next_due = None
    last_pass_at = None

    while True:
        try:
            now = datetime.now(timezone.utc)
            hint = delivery_service.get_next_due_hint()
            candidates = [
                value for value in (next_due, hint) if value is not None
            ]
            earliest = min(candidates) if candidates else None

            if delivery_service.should_run_delivery_pass(
                now,
                earliest,
                last_pass_at,
                quiet_staleness_seconds=DELIVERY_QUIET_STALENESS_SECONDS,
            ):
                await process_due_deliveries(
                    session_factory,
                    bot,
                )
                last_pass_at = datetime.now(timezone.utc)
                next_due = await delivery_service.get_next_due_at(
                    session_factory
                )
                delivery_service.clear_next_due_hint()
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
app.include_router(dashboard_api_router)
app.include_router(chat_router)
app.include_router(dashboard_page_router)

# Middleware added last runs outermost: security headers wrap
# everything, including rate-limit 429 responses.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    """
    Consistent error shape for the API: /api/* errors return
    {"error": "..."} instead of FastAPI's {"detail": "..."}.
    Other paths keep the default shape.
    """
    if request.url.path.startswith("/api/"):
        detail = (
            exc.detail
            if isinstance(exc.detail, str)
            else "Request failed."
        )
        return JSONResponse(
            {"error": detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "running",
        "docs": "/docs",
    }
