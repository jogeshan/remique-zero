
import logging
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from app.database import get_session_factory
from app.services.admin_service import (
    create_admin_invite,
    revoke_admin_invite,
    get_user_statistics,
)

logger = logging.getLogger(__name__)
BANGLADESH_TZ = ZoneInfo("Asia/Dhaka")


async def invite_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "For security, create invitations in a private chat with the bot."
        )
        return

    try:
        max_uses = int(context.args[0]) if len(context.args) >= 1 else 1
        expiry_days = int(context.args[1]) if len(context.args) >= 2 else None

        if len(context.args) > 2:
            raise ValueError

        if not 1 <= max_uses <= 10000:
            raise ValueError

        if expiry_days is not None and not 1 <= expiry_days <= 3650:
            raise ValueError

    except ValueError:
        await message.reply_text(
            "Usage:\n"
            "/invite\n"
            "/invite 5\n"
            "/invite 5 7\n\n"
            "First number: maximum uses.\n"
            "Second number: expiry in days."
        )
        return

    session_factory = get_session_factory()

    try:
        async with session_factory() as session:
            invite = await create_admin_invite(
                session,
                telegram_user_id=user.id,
                max_uses=max_uses,
                expires_in_days=expiry_days,
            )

        if invite.expires_at is not None:
            expiry_local = invite.expires_at.astimezone(BANGLADESH_TZ)
            expiry_text = expiry_local.strftime(
                "%d %B %Y, %I:%M %p %Z"
            )
        else:
            expiry_text = "Never (no expiry)"

        await message.reply_text(
            "✅ Invitation created!\n\n"
            f"Code:\n`{invite.code}`\n\n"
            f"Maximum uses: {invite.max_uses}\n"
            f"Remaining uses: {invite.max_uses}\n"
            f"Expires: {expiry_text}\n\n"
            "Share this code privately. It will not be displayed again.",
            parse_mode="Markdown",
        )

    except PermissionError:
        await message.reply_text(
            "⛔ You do not have administrator permission."
        )

    except Exception:
        logger.exception("Invite creation failed.")
        await message.reply_text(
            "Unable to create the invitation. Please try again."
        )


async def revoke_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "For security, revoke invitations in a private chat."
        )
        return

    if len(context.args) != 1:
        await message.reply_text("Usage: /revoke INVITE_CODE")
        return

    session_factory = get_session_factory()

    try:
        async with session_factory() as session:
            revoked = await revoke_admin_invite(
                session,
                telegram_user_id=user.id,
                code=context.args[0],
            )

        if revoked:
            await message.reply_text("✅ Invitation successfully revoked.")
        else:
            await message.reply_text(
                "Invitation not found or already revoked."
            )

    except PermissionError:
        await message.reply_text(
            "⛔ You do not have administrator permission."
        )

    except Exception:
        logger.exception("Invite revocation failed.")
        await message.reply_text(
            "Unable to revoke the invitation. Please try again."
        )


async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "For privacy, user statistics are only available in private chat."
        )
        return

    session_factory = get_session_factory()

    try:
        async with session_factory() as session:
            stats = await get_user_statistics(session, user.id)

        await message.reply_text(
            "📊 Remique Zero — User Statistics\n\n"
            f"Total users: {stats['total']}\n"
            f"Active users: {stats['active']}\n"
            f"Disabled users: {stats['disabled']}\n"
            f"Administrators: {stats['admins']}"
        )

    except PermissionError:
        await message.reply_text(
            "⛔ You do not have administrator permission."
        )

    except Exception:
        logger.exception("User statistics failed.")
        await message.reply_text(
            "Unable to retrieve user statistics."
        )
