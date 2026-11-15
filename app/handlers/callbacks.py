"""
Callback-query handlers and the /settings command.

Two button families live here:

- Reminder action buttons (`rem:{action}:{occurrence_id}`),
  attached to delivered reminder messages by the delivery
  worker: Done, Snooze 1h, Snooze tomorrow.
- Settings buttons (`set:tz:{zone}` / `set:lang:{code}`),
  attached to the /settings message.

Every handler resolves the clicking user from their Telegram
ID and only ever touches that user's own data.
"""

import logging
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import settings as app_settings
from app.database import get_session_factory
from app.models import UserSetting
from app.services import memory_service, reminder_service

logger = logging.getLogger(__name__)


# --- reminder action buttons ------------------------------------

REMINDER_ACTIONS = ("done", "snooze60", "snooze1440")

SNOOZE_MINUTES = {
    "snooze60": 60,
    "snooze1440": 1440,
}

SNOOZE_NOTES = {
    "snooze60": "⏰ Snoozed for 1 hour",
    "snooze1440": "⏰ Snoozed until tomorrow",
}


def parse_reminder_callback(data: str | None):
    """
    Parse `rem:{action}:{occurrence_id}` callback data.

    Returns (action, occurrence UUID) or None for anything
    malformed or unknown.
    """
    if not data or not data.startswith("rem:"):
        return None

    parts = data.split(":", 2)

    if len(parts) != 3:
        return None

    _, action, raw_id = parts

    if action not in REMINDER_ACTIONS:
        return None

    try:
        occurrence_id = UUID(raw_id)
    except (ValueError, TypeError, AttributeError):
        return None

    return action, occurrence_id


async def reminder_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    parsed = parse_reminder_callback(query.data)

    if parsed is None:
        await query.answer("This button is no longer valid.")
        return

    action, occurrence_id = parsed

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            user = await memory_service.get_active_user_by_telegram_id(
                session,
                query.from_user.id,
            )

            if user is None:
                await query.answer(
                    "Your Remique Zero account is not active."
                )
                return

            pair = await reminder_service.get_reminder_occurrence(
                session,
                occurrence_id,
            )

            if pair is None:
                await query.answer(
                    "That reminder is no longer available."
                )
                return

            _, reminder = pair

            if reminder.user_id != user.id:
                await query.answer("This isn't your reminder.")
                return

            if action == "done":
                changed = await reminder_service.mark_occurrence_done(
                    session,
                    user.id,
                    occurrence_id,
                )
                note = "✅ Done"
            else:
                changed = (
                    await reminder_service.snooze_delivered_occurrence(
                        session,
                        user.id,
                        occurrence_id,
                        SNOOZE_MINUTES[action],
                    )
                )
                note = SNOOZE_NOTES[action]

        if not changed:
            await query.answer(
                "That reminder can't be changed anymore."
            )
            return

        message = query.message

        if message is not None and message.text:
            try:
                await query.edit_message_text(
                    text=f"{message.text}\n\n{note}",
                    reply_markup=None,
                )
            except Exception:
                logger.debug(
                    "Unable to edit reminder message after %s.",
                    action,
                    exc_info=True,
                )

        await query.answer(note)

    except Exception:
        logger.exception(
            "Reminder callback failed for user %s",
            query.from_user.id,
        )

        try:
            await query.answer(
                "Something went wrong. Please try again."
            )
        except Exception:
            pass


# --- settings -----------------------------------------------------

TIMEZONE_CHOICES = [
    "Asia/Dhaka",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Europe/London",
    "America/New_York",
    "UTC",
]

LANGUAGE_CHOICES = ["en", "bn"]

LANGUAGE_LABELS = {
    "en": "English",
    "bn": "বাংলা (Bangla)",
}


def parse_settings_callback(data: str | None):
    """
    Parse `set:tz:{zone}` / `set:lang:{code}` callback data.

    Returns ("tz", zone) or ("lang", code), or None for anything
    malformed or not among the offered choices.
    """
    if not data or not data.startswith("set:"):
        return None

    parts = data.split(":", 2)

    if len(parts) != 3:
        return None

    _, kind, value = parts

    if kind == "tz" and value in TIMEZONE_CHOICES:
        return "tz", value

    if kind == "lang" and value in LANGUAGE_CHOICES:
        return "lang", value

    return None


def settings_keyboard(
    timezone_name: str,
    language: str,
) -> InlineKeyboardMarkup:
    """Build the settings buttons, marking the current values."""

    def tz_button(zone: str) -> InlineKeyboardButton:
        label = f"✓ {zone}" if zone == timezone_name else zone
        return InlineKeyboardButton(
            label,
            callback_data=f"set:tz:{zone}",
        )

    rows = [
        [tz_button(zone) for zone in TIMEZONE_CHOICES[:2]],
        [tz_button(zone) for zone in TIMEZONE_CHOICES[2:4]],
        [tz_button(zone) for zone in TIMEZONE_CHOICES[4:]],
        [
            InlineKeyboardButton(
                ("✓ " if language == code else "")
                + LANGUAGE_LABELS[code],
                callback_data=f"set:lang:{code}",
            )
            for code in LANGUAGE_CHOICES
        ],
    ]

    return InlineKeyboardMarkup(rows)


def format_settings_text(timezone_name: str, language: str) -> str:
    return (
        "⚙️ Your settings\n\n"
        f"🕐 Timezone: {timezone_name}\n"
        f"🗣 Language: {LANGUAGE_LABELS.get(language, language)}\n\n"
        "Tap a button to change a value. Reminder and expense "
        "times follow your timezone."
    )


async def _get_or_create_user_setting(session, user_id) -> UserSetting:
    """
    Load the user's settings row, creating it with the defaults
    when registration never made one (mirrors the row created in
    auth_service.register_telegram_user).
    """
    row = await session.get(UserSetting, user_id)

    if row is None:
        row = UserSetting(
            user_id=user_id,
            timezone=app_settings.default_timezone,
        )
        session.add(row)
        await session.flush()

    return row


async def settings_command(
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
            "Please use /settings in a private conversation."
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

            row = await session.get(UserSetting, account.id)
            timezone_name = (
                row.timezone
                if row is not None
                else app_settings.default_timezone
            )
            language = row.language if row is not None else "en"

        await message.reply_text(
            format_settings_text(timezone_name, language),
            reply_markup=settings_keyboard(timezone_name, language),
        )

    except Exception:
        logger.exception(
            "Unable to load settings for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't load your settings. Please try again."
        )


async def settings_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    parsed = parse_settings_callback(query.data)

    if parsed is None:
        await query.answer("This button is no longer valid.")
        return

    kind, value = parsed

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            user = await memory_service.get_active_user_by_telegram_id(
                session,
                query.from_user.id,
            )

            if user is None:
                await query.answer(
                    "Your Remique Zero account is not active."
                )
                return

            row = await _get_or_create_user_setting(session, user.id)

            if kind == "tz":
                row.timezone = value
                note = f"Timezone set to {value}."
            else:
                row.language = value
                note = f"Language set to {LANGUAGE_LABELS[value]}."

            await session.commit()
            timezone_name = row.timezone
            language = row.language

        try:
            await query.edit_message_text(
                text=format_settings_text(timezone_name, language),
                reply_markup=settings_keyboard(
                    timezone_name,
                    language,
                ),
            )
        except Exception:
            logger.debug(
                "Unable to edit settings message.",
                exc_info=True,
            )

        await query.answer(note)

    except Exception:
        logger.exception(
            "Settings callback failed for user %s",
            query.from_user.id,
        )

        try:
            await query.answer(
                "Something went wrong. Please try again."
            )
        except Exception:
            pass
