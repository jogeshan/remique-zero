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
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.config import settings as app_settings
from app.database import get_session_factory
from app.handlers.ai import _get_user_timezone_name, _user_zone
from app.handlers.callbacks import format_settings_text, settings_keyboard
from app.handlers.finance import _parse_amount_note
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

QUICKADD_TEXT = (
    "➕ <b>Quick Add</b>\n\n"
    "Tap what you want to add — I'll ask for the details, "
    "and your next message becomes the entry."
)

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
            [
                _btn("➕ Quick Add", "menu:quickadd"),
                _btn("❓ Help", "menu:help"),
            ],
        ]
    )


def quickadd_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _btn("💸 Expense", "menu:add:expense"),
                _btn("💰 Income", "menu:add:income"),
            ],
            [
                _btn("⏰ Reminder", "menu:add:reminder"),
                _btn("✅ Task", "menu:add:task"),
            ],
            _back_row(),
        ]
    )


def money_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _btn("➕ Add expense", "menu:add:expense"),
                _btn("➕ Add income", "menu:add:income"),
            ],
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
            [_btn("➕ New reminder", "menu:add:reminder")],
            [_btn("📋 My reminders", "menu:act:reminders")],
            _back_row(),
        ]
    )


def tasks_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("➕ New task", "menu:add:task")],
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
    "menu:quickadd": (QUICKADD_TEXT, quickadd_menu_keyboard),
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


# --- quick add --------------------------------------------------
#
# Tapping a quick-add button stores the kind in
# context.user_data["quick_add"] and prompts the user; their
# next plain-text message is then captured by
# quick_add_message_handler (wired ahead of the AI handler)
# and saved through the same services the slash commands use.

QUICK_ADD_KINDS = ("expense", "income", "reminder", "task")

QUICK_ADD_PROMPTS = {
    "expense": (
        "💸 <b>Add expense</b>\n\n"
        "Send the amount and what it was for, e.g. "
        "<code>250 lunch</code>.\n\n"
        "It's saved under the \"Other\" category — for automatic "
        "categories, just chat with me normally instead."
    ),
    "income": (
        "💰 <b>Add income</b>\n\n"
        "Send the amount and where it came from, e.g. "
        "<code>45000 salary</code>.\n\n"
        "It's saved under the \"Other\" category — for automatic "
        "categories, just chat with me normally instead."
    ),
    "reminder": (
        "⏰ <b>New reminder</b>\n\n"
        "Send what to remind you about and when, e.g. "
        "<code>call Rahim tomorrow at 9am</code> or "
        "<code>submit the report in 2 hours</code>."
    ),
    "task": (
        "✅ <b>New task</b>\n\n"
        "Send the task, e.g. <code>book the train tickets</code>."
    ),
}


def quick_add_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn("❌ Cancel", "menu:add:cancel")]]
    )


def parse_quick_add_callback(data: str | None):
    """
    Parse `menu:add:{kind}` / `menu:add:cancel` callback data.

    Returns the kind, "cancel", or None for anything unknown.
    """
    if not data or not data.startswith("menu:add:"):
        return None

    kind = data.rsplit(":", 1)[-1]

    if kind == "cancel" or kind in QUICK_ADD_KINDS:
        return kind

    return None


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

    quick_add = parse_quick_add_callback(data)

    if quick_add is not None:
        user_data = getattr(context, "user_data", None)

        if not isinstance(user_data, dict):
            await query.answer("Please try again.")
            return

        if quick_add == "cancel":
            user_data.pop("quick_add", None)
            await _edit_menu(query, MAIN_TEXT, main_menu_keyboard())
            await query.answer("Quick add cancelled.")
            return

        user_data["quick_add"] = quick_add
        await _edit_menu(
            query,
            QUICK_ADD_PROMPTS[quick_add],
            quick_add_cancel_keyboard(),
        )
        await query.answer("Type your entry as the next message.")
        return

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


async def _quick_add_money(session, account, kind, text, tz):
    """
    Save an expense/income from "amount note" text.

    Returns the confirmation message, or None when the text has
    no valid amount (the caller keeps the quick-add state so
    the user can retry).
    """
    parsed = _parse_amount_note(text.split())

    if parsed is None:
        return None

    amount, note = parsed

    await finance_service.add_transaction(
        session,
        account.id,
        type=kind,
        amount=amount,
        category="Other",
        note=note,
    )

    since, until = finance_service.period_range(
        "today",
        tz,
        datetime.now(timezone.utc),
    )
    summary = await finance_service.summarize(
        session,
        account.id,
        since=since,
        until=until,
    )

    icon = "💸" if kind == "expense" else "💰"
    label = "Expense" if kind == "expense" else "Income"
    fmt = finance_service.format_money

    confirmation = f"{icon} {label} added: {fmt(amount)} BDT"
    if note:
        confirmation += f" — {note}"
    confirmation += (
        f"\nToday: spent {fmt(summary['total_expense'])} · "
        f"earned {fmt(summary['total_income'])} (BDT)"
    )

    return confirmation


async def _quick_add_reminder(session, account, text, tz, tz_name):
    """
    Create a reminder from free text.

    Returns the confirmation message, or None when no date or
    time could be found (the caller keeps the quick-add state).
    """
    parsed = reminder_service.parse_reminder_text(
        text,
        tz,
        datetime.now(tz),
    )

    if parsed is None:
        return None

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
    confirmation = f"⏰ Reminder set: {title}\nWhen: {when} ({tz_name})"

    label = reminder_service.recurrence_label(reminder.recurrence_rule)
    if label:
        confirmation += f"\nRepeats: {label}"
    confirmation += f"\n(id: {str(reminder.id)[:8]})"

    return confirmation


async def quick_add_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Capture the message that follows a quick-add button tap.

    Returns True when the message was consumed as a quick-add
    entry (or a retry prompt was sent), False when no quick
    add is pending and the message should flow on to the AI.
    """
    user_data = getattr(context, "user_data", None)

    if not isinstance(user_data, dict):
        return False

    kind = user_data.get("quick_add")

    if kind not in QUICK_ADD_KINDS:
        return False

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return False

    if chat.type != "private":
        return False

    text = (message.text or "").strip()

    if not text:
        return False

    if text.lower() == "cancel":
        user_data.pop("quick_add", None)
        await message.reply_text("Quick add cancelled.")
        return True

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await memory_service.get_active_user_by_telegram_id(
                session,
                user.id,
            )

            if account is None:
                user_data.pop("quick_add", None)
                await message.reply_text(
                    "Your Remique Zero account is not active. "
                    "Please contact the administrator."
                )
                return True

            tz_name = await _get_user_timezone_name(session, account.id)
            tz = _user_zone(tz_name)

            if kind in ("expense", "income"):
                confirmation = await _quick_add_money(
                    session,
                    account,
                    kind,
                    text,
                    tz,
                )

                if confirmation is None:
                    await message.reply_text(
                        "I need an amount first, e.g. \"250 lunch\". "
                        "Try again, or send /cancel to stop."
                    )
                    return True

            elif kind == "reminder":
                confirmation = await _quick_add_reminder(
                    session,
                    account,
                    text,
                    tz,
                    tz_name,
                )

                if confirmation is None:
                    await message.reply_text(
                        "I couldn't find a date or time in that. "
                        "Try something like \"tomorrow at 9am\" or "
                        "\"in 2 hours\" — or send /cancel to stop."
                    )
                    return True

            else:  # task
                title = " ".join(text.split())[:200]
                await task_service.create_task(
                    session,
                    account.id,
                    title=title,
                )
                confirmation = f"✅ Task added: {title}"

        user_data.pop("quick_add", None)
        await message.reply_text(confirmation)
        return True

    except Exception:
        logger.exception(
            "Quick add (%s) failed for user %s",
            kind,
            user.id,
        )

        await message.reply_text(
            "I couldn't save that. Please try again."
        )
        return True


async def text_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Plain-text dispatch: a pending quick add wins over the AI."""
    if await quick_add_message_handler(update, context):
        return

    from app.handlers.ai import ai_message_handler

    await ai_message_handler(update, context)


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Cancel a pending quick add (no-op message otherwise)."""
    message = update.effective_message

    if not message:
        return

    user_data = getattr(context, "user_data", None)
    pending = (
        isinstance(user_data, dict)
        and user_data.pop("quick_add", None) is not None
    )

    if pending:
        await message.reply_text("Quick add cancelled.")
    else:
        await message.reply_text("Nothing to cancel.")
