import logging

from app.services.admin_service import get_admin
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database import get_session_factory
from app.services.auth_service import (
    register_telegram_user,
    InvalidRegistrationInvite,
    TelegramAccountAlreadyLinked,
    RegistrationError,
)

logger = logging.getLogger(__name__)


async def start_command(
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
            "Please register through a private conversation with Remique Zero."
        )
        return

    if len(context.args) > 1:
        await message.reply_text(
            "Invalid registration command. Please use your invitation link."
        )
        return

    invite_code = context.args[0] if context.args else None

    session_factory = get_session_factory()

    try:
        async with session_factory() as session:
            result = await register_telegram_user(
                session,
                invite_code=invite_code,
                telegram_user_id=user.id,
                chat_id=chat.id,
                username=user.username,
                display_name=user.full_name,
            )

        if result.role == "super_admin":
            welcome = (
                "👑 Welcome, Super Administrator!\n\n"
                "Your Remique Zero account is active.\n\n"
                "Available administrator commands:\n"
                "/invite — Create an invitation\n"
                "/revoke — Revoke an invitation\n"
                "/users — View user statistics\n"
                "/help — Show help"
            )
        else:
            welcome = (
                "🎉 Welcome to Remique Zero!\n\n"
                "Your account has been successfully activated.\n\n"
                "Type /help to explore available commands."
            )

        await message.reply_text(welcome)

        logger.info(
            "Telegram registration completed: user_id=%s role=%s",
            result.user_id,
            result.role,
        )

    except InvalidRegistrationInvite:
        await message.reply_text(
            "❌ A valid invitation is required to join Remique Zero.\n\n"
            "Your invitation may be invalid, expired, revoked, "
            "or already fully used.\n\n"
            "Please contact the administrator."
        )

    except TelegramAccountAlreadyLinked:
        await message.reply_text(
            "👋 Welcome back to Remique Zero!\n\n"
            "Your account is already active. Here's what I can do:\n\n"
            "💬 Chat — just type, or send photos, voice messages "
            "and documents for me to read\n"
            "⏰ Reminders — /remind, or simply ask me in chat\n"
            "🧾 Expenses — log spending and income, and track "
            "debts and loans (/summary, /debts)\n"
            "🧠 Memory — ask me to remember things for you "
            "(/memories)\n\n"
            "Type /help for all commands."
        )

    except RegistrationError:
        await message.reply_text(
            "Registration could not be completed. Please contact the administrator."
        )

    except Exception:
        logger.exception("Unexpected Telegram registration error.")
        await message.reply_text(
            "Something went wrong. Please try again later."
        )




async def help_command(update, context):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "Please use /help in a private chat with Remique Zero."
        )
        return

    session_factory = get_session_factory()

    async with session_factory() as session:
        admin = await get_admin(session, user.id)

    if admin is None:
        help_text = """🤖 <b>Remique Zero — Commands</b>

<b>💬 General</b>
🚀 /start — Register or open your account
📋 /menu — Interactive menu with buttons
❓ /help — Show commands
💬 /newchat — Start a new conversation
🧠 /memories — List what I remember about you

<b>⏰ Reminders &amp; Tasks</b>
⏰ /remind TEXT — Set a reminder
📋 /reminders — List your reminders
✅ /tasks — Your personal task list

<b>💰 Money</b>
💸 /expense AMOUNT NOTE — Quick-add an expense
💰 /income AMOUNT NOTE — Quick-add income
📊 /summary — Yesterday's money overview (or /summary today|week|month)
🤝 /debts — Your open debts and loans

<b>⚙️ Account</b>
⚙️ /settings — Timezone and language
🔑 /apitoken — Get a token for the web dashboard
📥 /export — Download your data as a JSON file

📸 You can also send me photos, voice messages and documents (PDF/text) and I will read them.

Your personal assistant is ready to help you."""

    elif admin.role == "super_admin":
        help_text = """👑 <b>Remique Zero — Super Admin</b>

<b>💬 General</b>
🚀 /start — Register or open your account
📋 /menu — Interactive menu with buttons
❓ /help — Show commands
💬 /newchat — Start a new conversation
🧠 /memories — List what I remember about you

<b>⏰ Reminders &amp; Tasks</b>
⏰ /remind TEXT — Set a reminder
📋 /reminders — List your reminders
✅ /tasks — Your personal task list

<b>💰 Money</b>
💸 /expense AMOUNT NOTE — Quick-add an expense
💰 /income AMOUNT NOTE — Quick-add income
📊 /summary — Yesterday's money overview (or /summary today|week|month)
🤝 /debts — Your open debts and loans

<b>⚙️ Account</b>
⚙️ /settings — Timezone and language
🔑 /apitoken — Get a token for the web dashboard
📥 /export — Download your data as a JSON file

📸 You can also send me photos, voice messages and documents (PDF/text) and I will read them.

<b>🛡️ Administration</b>
✉️ /invite [uses] [expiry_days] — Generate invitation
🚫 /revoke CODE — Revoke invitation
👥 /users — View user statistics
🔒 /disable TELEGRAM_ID — Disable account
🔓 /enable TELEGRAM_ID — Reactivate account

<b>👑 Super Admin Only</b>
⬆️ /promote TELEGRAM_ID — Make admin
⬇️ /demote TELEGRAM_ID — Remove admin role"""

    else:
        help_text = """🛡️ <b>Remique Zero — Administrator</b>

<b>💬 General</b>
🚀 /start — Register or open your account
📋 /menu — Interactive menu with buttons
❓ /help — Show commands
💬 /newchat — Start a new conversation
🧠 /memories — List what I remember about you

<b>⏰ Reminders &amp; Tasks</b>
⏰ /remind TEXT — Set a reminder
📋 /reminders — List your reminders
✅ /tasks — Your personal task list

<b>💰 Money</b>
💸 /expense AMOUNT NOTE — Quick-add an expense
💰 /income AMOUNT NOTE — Quick-add income
📊 /summary — Yesterday's money overview (or /summary today|week|month)
🤝 /debts — Your open debts and loans

<b>⚙️ Account</b>
⚙️ /settings — Timezone and language
🔑 /apitoken — Get a token for the web dashboard
📥 /export — Download your data as a JSON file

📸 You can also send me photos, voice messages and documents (PDF/text) and I will read them.

<b>🛡️ Administration</b>
✉️ /invite [uses] [expiry_days] — Generate invitation
🚫 /revoke CODE — Revoke invitation
👥 /users — View user statistics
🔒 /disable TELEGRAM_ID — Disable account
🔓 /enable TELEGRAM_ID — Reactivate account"""

    await message.reply_text(help_text, parse_mode=ParseMode.HTML)
