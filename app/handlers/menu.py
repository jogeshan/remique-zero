"""
Interactive inline menu (/menu) for Remique Zero.

The menu is a single message the user navigates with inline
buttons (`menu:*` callbacks). Section buttons swap the message
to a submenu; action buttons swap it to live content (summary,
reminders, tasks, debts, settings) built from the same services
the slash commands use, always scoped to the clicking user.
"""

import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import settings as app_settings
from app.database import get_session_factory
from app.handlers.ai import _get_user_timezone_name, _user_zone
from app.handlers.callbacks import format_settings_text, settings_keyboard
from app.models import UserSetting
from app.services import (
    finance_service,
    memory_service,
    reminder_service,
    task_service,
)

logger = logging.getLogger(__name__)

MAX_LIST_ITEMS = 10

MAIN_TEXT = (
    "📋 <b>Remique Zero — Main Menu</b>\n\n"
    "Tap a section to open it. You can also just talk to me "
    "in plain language, or type /help for all commands."
)

MONEY_TEXT = (
    "💰 <b>Money</b>\n\n"
    "Log spending or income by typing in chat, e.g. "
    "\"spent 200 on food\" or \"got salary 30000\"."
)

REMINDERS_TEXT = (
    "⏰ <b>Reminders</b>\n\n"
    "Create one by typing in chat, e.g. \"remind me to call "
    "Rahim tomorrow at 9am\", or with /remind."
)

TASKS_TEXT = (
    "✅ <b>Tasks</b>\n\n"
    "Add to your list by typing in chat, e.g. \"add to my "
    "list: book the train tickets\"."
)

ACCOUNT_TEXT = "⚙️ <b>Account</b>"

HELP_TEXT = (
    "❓ <b>Help — the essentials</b>\n\n"
    "💬 Just chat — I remember, answer and act on what you say.\n"
    "⏰ \"remind me to …\" — set reminders in plain language.\n"
    "💰 \"spent 200 on food\" — log money in plain language.\n"
    "✅ \"add to my list: …\" — track tasks.\n\n"
    "Type /help for the full command list."
)


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _back_row(target: str = "menu:main") -> list[InlineKeyboardButton]:
    return [_btn("⬅️ Back", target)]


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _btn("💰 Money", "menu:money"),
                _btn("⏰ Reminders", "menu:reminders"),
            ],
            [
                _btn("✅ Tasks", "menu:tasks"),
                _btn("⚙️ Account", "menu:account"),
            ],
            [_btn("❓ Help", "menu:help")],
        ]
    )


def money_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _btn("📊 Yesterday's summary", "menu:act:summary"),
                _btn("🤝 Debts & loans", "menu:act:debts"),
            ],
            _back_row(),
        ]
    )


def reminders_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("📋 My reminders", "menu:act:reminders")],
            _back_row(),
        ]
    )


def tasks_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("📋 My tasks", "menu:act:tasks")],
            _back_row(),
        ]
    )


def account_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("⚙️ Settings", "menu:act:settings")],
            [_btn("🧠 My memories — /memories", "menu:act:memories")],
            _back_row(),
        ]
    )


def content_keyboard(section: str) -> InlineKeyboardMarkup:
    """Back button returning to the section an action came from."""
    return InlineKeyboardMarkup([_back_row(f"menu:{section}")])


# Screens reachable by plain navigation: data -> (text, keyboard)
NAV_SCREENS = {
    "menu:main": (MAIN_TEXT, main_menu_keyboard),
    "menu:money": (MONEY_TEXT, money_menu_keyboard),
    "menu:reminders": (REMINDERS_TEXT, reminders_menu_keyboard),
    "menu:tasks": (TASKS_TEXT, tasks_menu_keyboard),
    "menu:account": (ACCOUNT_TEXT, account_menu_keyboard),
    "menu:help": (HELP_TEXT, lambda: InlineKeyboardMarkup([_back_row()])),
}

ACTION_SECTIONS = {
    "menu:act:summary": "money",
    "menu:act:debts": "money",
    "menu:act:reminders": "reminders",
    "menu:act:tasks": "tasks",
    "menu:act:settings": "account",
    "menu:act:memories": "account",
}


async def _summary_text(session, user_id, tz) -> str:
    now = datetime.now(timezone.utc)
    return await finance_service.build_yesterday_overview(
        session,
        user_id,
        tz,
        now,
    )


async def _debts_text(session, user_id) -> str:
    debts = await finance_service.list_debts(session, user_id)

    if not debts:
        return (
            "🤝 <b>Debts &amp; loans</b>\n\n"
            "You have no open debts or loans. 🎉\n\n"
            "Tell me in chat when you borrow or lend money, "
            "like \"borrowed 5000 from Rahim\"."
        )

    totals = await finance_service.debt_totals(session, user_id)
    fmt = finance_service.format_money
    lines = ["🤝 <b>Your open debts &amp; loans</b>"]

    borrowed = [d for d in debts if d.direction == "borrowed"]
    lent = [d for d in debts if d.direction == "lent"]

    if borrowed:
        lines.append("\nYou owe:")
        for debt in borrowed[:MAX_LIST_ITEMS]:
            lines.append(
                f"• {debt.counterparty} — {fmt(debt.remaining)} BDT "
                f"left (of {fmt(debt.principal)})"
            )

    if lent:
        lines.append("\nOwed to you:")
        for debt in lent[:MAX_LIST_ITEMS]:
            lines.append(
                f"• {debt.counterparty} — {fmt(debt.remaining)} BDT "
                f"left (of {fmt(debt.principal)})"
            )

    lines.append(
        f"\nTotals: you owe {fmt(totals['owed_by_user'])} · "
        f"owed to you {fmt(totals['owed_to_user'])} (BDT)"
    )

    return "\n".join(lines)


async def _reminders_text(session, user_id, tz) -> str:
    pairs = await reminder_service.list_active_reminders(
        session,
        user_id,
    )

    if not pairs:
        return (
            "⏰ <b>Your reminders</b>\n\n"
            "You don't have any active reminders.\n\n"
            "Set one with /remind, for example:\n"
            "/remind tomorrow at 9am submit the report"
        )

    lines = ["⏰ <b>Your reminders</b>\n"]

    for number, (reminder, next_at) in enumerate(
        pairs[:MAX_LIST_ITEMS],
        start=1,
    ):
        if next_at is not None:
            when = next_at.astimezone(tz).strftime("%a %d %b %Y, %H:%M")
        else:
            when = "not scheduled"

        label = reminder_service.recurrence_label(reminder.recurrence_rule)
        repeat = f" [{label}]" if label else ""
        lines.append(f"{number}. {reminder.title}{repeat}\n   Next: {when}")

    if len(pairs) > MAX_LIST_ITEMS:
        lines.append(
            f"\n…and {len(pairs) - MAX_LIST_ITEMS} more — "
            "use /reminders for the full list."
        )

    return "\n".join(lines)


async def _tasks_text(session, user_id, tz) -> str:
    tasks = await task_service.list_tasks(session, user_id, status="open")

    if not tasks:
        return (
            "✅ <b>Your tasks</b>\n\n"
            "Your task list is empty. 🎉\n\n"
            "Tell me in chat, like \"add to my list: book the "
            "train tickets\", and I'll track it here."
        )

    lines = ["✅ <b>Your tasks</b>\n"]

    for number, task in enumerate(tasks[:MAX_LIST_ITEMS], start=1):
        line = f"{number}. {task.title}"

        if task.due_at is not None:
            due = task.due_at.astimezone(tz).strftime("%a %d %b %Y, %H:%M")
            line += f"\n   Due: {due}"

        lines.append(line)

    if len(tasks) > MAX_LIST_ITEMS:
        lines.append(
            f"\n…and {len(tasks) - MAX_LIST_ITEMS} more — "
            "use /tasks for the full list."
        )

    return "\n".join(lines)


async def _settings_screen(session, user_id):
    row = await session.get(UserSetting, user_id)
    timezone_name = (
        row.timezone if row is not None else app_settings.default_timezone
    )
    language = row.language if row is not None else "en"
    return format_settings_text(timezone_name, language), settings_keyboard(
        timezone_name,
        language,
    )


async def _memories_text(session, user_id) -> str:
    memories = await memory_service.list_memories(session, user_id)

    if not memories:
        return (
            "🧠 <b>Your memories</b>\n\n"
            "I haven't saved any memories for you yet.\n\n"
            "Tell me \"remember that …\" and I'll keep it here."
        )

    lines = ["🧠 <b>Your memories</b>\n"]

    for number, memory in enumerate(memories[:MAX_LIST_ITEMS], start=1):
        text = " ".join(memory.content.split())
        if len(text) > 120:
            text = text[:117].rstrip() + "…"
        lines.append(f"{number}. {text}")

    if len(memories) > MAX_LIST_ITEMS:
        lines.append(
            f"\n…and {len(memories) - MAX_LIST_ITEMS} more — "
            "use /memories for the full list."
        )

    return "\n".join(lines)


async def _edit_menu(query, text: str, keyboard) -> None:
    from telegram.constants import ParseMode

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.debug("Unable to edit menu message.", exc_info=True)


async def menu_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    from telegram.constants import ParseMode

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "Please use /menu in a private conversation."
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

        await message.reply_text(
            MAIN_TEXT,
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        logger.exception("Unable to open menu for user %s", user.id)

        await message.reply_text(
            "I couldn't open the menu. Please try again."
        )


async def menu_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    data = query.data or ""

    if data in NAV_SCREENS:
        text, build_keyboard = NAV_SCREENS[data]
        await _edit_menu(query, text, build_keyboard())
        await query.answer()
        return

    if data not in ACTION_SECTIONS:
        await query.answer("This button is no longer valid.")
        return

    section = ACTION_SECTIONS[data]

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

            tz_name = await _get_user_timezone_name(session, user.id)
            tz = _user_zone(tz_name)

            if data == "menu:act:summary":
                text = await _summary_text(session, user.id, tz)
                keyboard = content_keyboard(section)
            elif data == "menu:act:debts":
                text = await _debts_text(session, user.id)
                keyboard = content_keyboard(section)
            elif data == "menu:act:reminders":
                text = await _reminders_text(session, user.id, tz)
                keyboard = content_keyboard(section)
            elif data == "menu:act:tasks":
                text = await _tasks_text(session, user.id, tz)
                keyboard = content_keyboard(section)
            elif data == "menu:act:memories":
                text = await _memories_text(session, user.id)
                keyboard = content_keyboard(section)
            else:  # menu:act:settings
                text, keyboard = await _settings_screen(session, user.id)

        await _edit_menu(query, text, keyboard)
        await query.answer()

    except Exception:
        logger.exception(
            "Menu callback failed for user %s",
            query.from_user.id,
        )

        try:
            await query.answer(
                "Something went wrong. Please try again."
            )
        except Exception:
            pass
