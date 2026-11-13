import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status
from telegram import Update

from app.config import settings


router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    secret_token: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
):
    expected_secret = settings.telegram_webhook_secret

    if not expected_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram webhook is not configured.",
        )

    if (
        not secret_token
        or not hmac.compare_digest(secret_token, expected_secret)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook secret.",
        )

    telegram_app = getattr(
        request.app.state,
        "telegram_application",
        None,
    )

    if telegram_app is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram application is not running.",
        )

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Telegram update.",
        )

    try:
        update = Update.de_json(payload, telegram_app.bot)
    except Exception:
        logger.exception("Failed to parse Telegram update")

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Telegram update.",
        )

    if update is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty Telegram update.",
        )

    try:
        await telegram_app.update_queue.put(update)

        logger.info(
            "Telegram update queued: update_id=%s",
            update.update_id,
        )

        return {"ok": True}

    except Exception:
        logger.exception("Failed to queue Telegram update")

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to queue Telegram update.",
        )