import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.config import settings
from app.database import get_session_factory
from app.models import UserSetting

from app.services.ai_service import (
    AIAccountInactive,
    AIConfigurationError,
    AIProviderError,
    AIQuotaExceeded,
    AIReply,
    AIServiceError,
    generate_ai_reply,
)

from app.services.conversation_service import (
    ConversationAccessError,
    get_active_conversation_context,
    save_conversation_exchange,
    start_new_conversation,
)

from app.handlers.text_utils import reply_long
from app.services import memory_service, reminder_service


logger = logging.getLogger(__name__)

MAX_INPUT_CHARACTERS = 8000


async def _send_long_message(message, text: str) -> None:
    await reply_long(message, text)


async def newchat_command(
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
            "Please use /newchat in a private conversation."
        )
        return

    try:
        session_factory = get_session_factory()

        await start_new_conversation(
            session_factory,
            user.id,
        )

        await message.reply_text(
            "✨ A new conversation has started.\n\n"
            "Your previous conversation is safely preserved. "
            "Let's start fresh!"
        )

    except ConversationAccessError:
        await message.reply_text(
            "Your Remique Zero account is not active. "
            "Please contact the administrator."
        )

    except Exception:
        logger.exception(
            "Unable to start new conversation for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't start a new conversation. Please try again."
        )


async def memories_command(
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
            "Please use /memories in a private conversation."
        )
        return

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await memory_service.get_active_user_by_telegram_id(
                session,
                user.id,
            )

            if account is None:
                await message.reply_text(
                    "Your Remique Zero account is not active. "
                    "Please contact the administrator."
                )
                return

            memories = await memory_service.list_memories(
                session,
                account.id,
            )

        if not memories:
            await message.reply_text(
                "I don't have any memories saved for you yet.\n\n"
                "Tell me something like \"remember that my favorite "
                "color is blue\" and I'll store it here."
            )
            return

        lines = ["🧠 What I remember about you:\n"]

        for number, memory in enumerate(memories, start=1):
            short_id = str(memory.id)[:8]
            content = " ".join(memory.content.split())
            if len(content) > 200:
                content = content[:197].rstrip() + "…"
            lines.append(
                f"{number}. [{memory.category}] {content} "
                f"(id: {short_id})"
            )

        lines.append(
            "\nTo remove one, just tell me \"forget that …\" or "
            "give me its number or id, and I'll delete it."
        )

        await _send_long_message(message, "\n".join(lines))

    except Exception:
        logger.exception(
            "Unable to list memories for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't load your memories. Please try again."
        )


async def _get_user_timezone_name(session, user_id) -> str:
    """Return the user's configured timezone name (or the default)."""
    tz_name = await session.scalar(
        select(UserSetting.timezone).where(
            UserSetting.user_id == user_id
        )
    )
    return tz_name or settings.default_timezone


def _user_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


async def remind_command(
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
            "Please use /remind in a private conversation."
        )
        return

    text = " ".join(context.args or []).strip()

    if not text:
        await message.reply_text(
            "Tell me what to remind you about and when.\n\n"
            "Examples:\n"
            "/remind tomorrow at 9am submit the report\n"
            "/remind in 2 hours call mom\n"
            "/remind every Sunday at 18:30 prepare the weekly report"
        )
        return

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await memory_service.get_active_user_by_telegram_id(
                session,
                user.id,
            )

            if account is None:
                await message.reply_text(
                    "Your Remique Zero account is not active. "
                    "Please contact the administrator."
                )
                return

            tz_name = await _get_user_timezone_name(session, account.id)
            tz = _user_zone(tz_name)
            now_local = datetime.now(tz)

            parsed = reminder_service.parse_reminder_text(
                text,
                tz,
                now_local,
            )

            if parsed is None:
                await message.reply_text(
                    "I couldn't find a date or time in that. "
                    "Try something like \"tomorrow at 9am\", "
                    "\"in 2 hours\" or \"every Sunday at 18:30\"."
                )
                return

            title, first_run_local, recurrence = parsed

            reminder = await reminder_service.create_reminder(
                session,
                account,
                title=title,
                first_run_at=first_run_local,
                recurrence=recurrence,
                timezone_name=tz_name,
            )

        when = first_run_local.strftime("%a %d %b %Y, %H:%M")
        label = reminder_service.recurrence_label(
            reminder.recurrence_rule
        )

        confirmation = f"⏰ Reminder set: {title}\nWhen: {when} ({tz_name})"
        if label:
            confirmation += f"\nRepeats: {label}"
        confirmation += f"\n(id: {str(reminder.id)[:8]})"

        await message.reply_text(confirmation)

    except Exception:
        logger.exception(
            "Unable to create reminder for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't set that reminder. Please try again."
        )


async def reminders_command(
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
            "Please use /reminders in a private conversation."
        )
        return

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await memory_service.get_active_user_by_telegram_id(
                session,
                user.id,
            )

            if account is None:
                await message.reply_text(
                    "Your Remique Zero account is not active. "
                    "Please contact the administrator."
                )
                return

            tz_name = await _get_user_timezone_name(session, account.id)
            tz = _user_zone(tz_name)

            pairs = await reminder_service.list_active_reminders(
                session,
                account.id,
            )

        if not pairs:
            await message.reply_text(
                "You don't have any active reminders.\n\n"
                "Set one with /remind, for example:\n"
                "/remind tomorrow at 9am submit the report"
            )
            return

        lines = ["⏰ Your reminders:\n"]

        for number, (reminder, next_at) in enumerate(pairs, start=1):
            short_id = str(reminder.id)[:8]
            if next_at is not None:
                when = next_at.astimezone(tz).strftime(
                    "%a %d %b %Y, %H:%M"
                )
            else:
                when = "not scheduled"

            label = reminder_service.recurrence_label(
                reminder.recurrence_rule
            )
            repeat = f" [{label}]" if label else ""

            lines.append(
                f"{number}. {reminder.title}{repeat}\n"
                f"   Next: {when} (id: {short_id})"
            )

        lines.append(
            "\nTo cancel or snooze one, just tell me in chat "
            "and give me its id."
        )

        await _send_long_message(message, "\n".join(lines))

    except Exception:
        logger.exception(
            "Unable to list reminders for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't load your reminders. Please try again."
        )


async def ai_message_handler(
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
            "Please chat with Remique Zero privately to use AI."
        )
        return

    prompt = (message.text or "").strip()

    if not prompt:
        return

    if len(prompt) > MAX_INPUT_CHARACTERS:
        await message.reply_text(
            "Your message is too long. "
            "Please send a shorter message."
        )
        return

    try:
        await context.bot.send_chat_action(
            chat_id=chat.id,
            action=ChatAction.TYPING,
        )
    except Exception:
        logger.debug("Unable to send Telegram typing indicator.")

    session_factory = get_session_factory()

    try:
        conversation_id, history = (
            await get_active_conversation_context(
                session_factory,
                user.id,
            )
        )

        result: AIReply = await generate_ai_reply(
            session_factory,
            telegram_user_id=user.id,
            prompt=prompt,
            history=history,
        )

        try:
            await save_conversation_exchange(
                session_factory,
                telegram_user_id=user.id,
                conversation_id=conversation_id,
                prompt=prompt,
                response=result.text,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        except Exception:
            logger.exception(
                "AI response generated but conversation persistence failed "
                "for Telegram user %s",
                user.id,
            )

            await _send_long_message(message, result.text)

            await message.reply_text(
                "⚠️ Your response was generated, but I couldn't save "
                "this exchange to conversation history."
            )
            return

        await _send_long_message(message, result.text)

    except (AIAccountInactive, ConversationAccessError):
        await message.reply_text(
            "Your Remique Zero account is not active. "
            "Please contact the administrator."
        )

    except AIQuotaExceeded:
        await message.reply_text(
            f"You've reached your daily AI request limit of "
            f"{settings.max_ai_requests_per_user_per_day}.\n\n"
            "Your allowance resets at midnight in your account timezone."
        )

    except AIConfigurationError:
        logger.error("Gemini API configuration is incomplete.")

        await message.reply_text(
            "The AI service is not configured correctly. "
            "Please contact the administrator."
        )

    except AIProviderError:
        await message.reply_text(
            "I'm temporarily unable to process your message. "
            "Please try again later."
        )

    except AIServiceError as exc:
        await message.reply_text(str(exc))

    except Exception:
        logger.exception(
            "Unexpected AI handler error for Telegram user %s",
            user.id,
        )

        await message.reply_text(
            "Something went wrong while processing your message."
        )