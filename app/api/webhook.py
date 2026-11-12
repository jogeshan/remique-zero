import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status

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
    expected = settings.telegram_webhook_secret

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram webhook is not configured.",
        )

    if not secret_token or not hmac.compare_digest(secret_token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook secret.",
        )

    # Do not activate Telegram webhook until update processing is implemented.
    update = await request.json()

    logger.info(
        "Telegram update received: update_id=%s",
        update.get("update_id"),
    )

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Telegram update processing is not enabled yet.",
    )
