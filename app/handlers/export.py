import io
import json
import logging
from datetime import datetime, timezone

from telegram import InputFile, Update
from telegram.ext import ContextTypes

from app.database import get_session_factory
from app.services import export_service, memory_service

logger = logging.getLogger(__name__)


async def export_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    /export — send the user a JSON file with all of their data.

    The export is built for the account linked to the sender's
    Telegram ID, so a user can only ever export their own data.
    The document contents are never logged.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "Please use /export in a private conversation — "
            "the file contains your personal data."
        )
        return

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = (
                await memory_service.get_active_user_by_telegram_id(
                    session,
                    user.id,
                )
            )

            if account is None:
                await message.reply_text(
                    "Your Remique Zero account is not active. "
                    "Please contact the administrator."
                )
                return

            document = await export_service.build_user_export(
                session,
                account,
            )
    except Exception:
        logger.exception("Failed to build a data export.")
        await message.reply_text(
            "I couldn't build your export. Please try again."
        )
        return

    payload = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    filename = (
        "remique-zero-export-"
        f"{datetime.now(timezone.utc):%Y%m%d}.json"
    )

    await message.reply_document(
        document=InputFile(io.BytesIO(payload), filename=filename),
        caption=(
            "📦 Your Remique Zero data export — profile, settings, "
            "memories, reminders, transactions, debts and tasks "
            "in one JSON file. Keep it private: it contains your "
            "personal data."
        ),
    )
