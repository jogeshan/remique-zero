import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.database import get_session_factory
from app.services.user_management_service import (
    set_user_status,
    change_user_role,
)

logger = logging.getLogger(__name__)


async def _manage_account(update, context, operation):
    message = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat

    if not message or not actor or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "For security, account management is only available in private chat."
        )
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        await message.reply_text(
            f"Usage: /{operation} TELEGRAM_USER_ID"
        )
        return

    target_id = int(context.args[0])

    if target_id <= 0:
        await message.reply_text("Invalid Telegram user ID.")
        return

    session_factory = get_session_factory()

    try:
        async with session_factory() as session:

            if operation in ("disable", "enable"):
                result = await set_user_status(
                    session,
                    actor_telegram_id=actor.id,
                    target_telegram_id=target_id,
                    new_status=(
                        "disabled" if operation == "disable" else "active"
                    ),
                )
            else:
                result = await change_user_role(
                    session,
                    actor_telegram_id=actor.id,
                    target_telegram_id=target_id,
                    new_role=(
                        "admin" if operation == "promote" else "user"
                    ),
                )

        if result:
            await message.reply_text(
                f"✅ Account updated successfully: {operation}"
            )
        else:
            await message.reply_text(
                "No change made. The account may not exist or may already have that setting."
            )

    except PermissionError as exc:
        await message.reply_text(f"⛔ {exc}")

    except Exception:
        logger.exception("User management operation failed.")
        await message.reply_text(
            "Unable to complete this operation. Please try again."
        )


async def disable_command(update, context):
    await _manage_account(update, context, "disable")


async def enable_command(update, context):
    await _manage_account(update, context, "enable")


async def promote_command(update, context):
    await _manage_account(update, context, "promote")


async def demote_command(update, context):
    await _manage_account(update, context, "demote")
