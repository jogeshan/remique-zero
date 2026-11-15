import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app.config import settings
from app.database import get_session_factory
from app.models import UserSetting
from app.handlers.text_utils import reply_long
from app.services import finance_service, memory_service

logger = logging.getLogger(__name__)


async def _send_long_message(message, text: str) -> None:
    await reply_long(message, text)


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


async def _resolve_account(message, session, telegram_user_id):
    """
    Resolve the active account for a finance command, replying
    with the standard not-active message when there is none.
    Returns the account, or None after replying.
    """
    account = await memory_service.get_active_user_by_telegram_id(
        session,
        telegram_user_id,
    )

    if account is None:
        await message.reply_text(
            "Your Remique Zero account is not active. "
            "Please contact the administrator."
        )
        return None

    return account


def _parse_amount_note(args) -> tuple[Decimal, str | None] | None:
    """
    Parse "/expense 250 lunch with friends" arguments.

    The first token is the amount; the rest is the note. Returns
    None when there is no valid positive amount.
    """
    if not args:
        return None

    try:
        amount = finance_service.parse_amount(args[0])
    except ValueError:
        return None

    note = " ".join(args[1:]).strip() or None
    return amount, note


async def _quick_add_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    transaction_type: str,
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    command = "expense" if transaction_type == "expense" else "income"

    if chat.type != "private":
        await message.reply_text(
            f"Please use /{command} in a private conversation."
        )
        return

    parsed = _parse_amount_note(context.args or [])

    if parsed is None:
        example = (
            "/expense 250 lunch"
            if transaction_type == "expense"
            else "/income 45000 salary"
        )
        await message.reply_text(
            f"Usage: /{command} <amount> [note]\n"
            f"Example: {example}\n\n"
            "Or just tell me in chat, like \"lunch 250 taka\" — "
            "I'll pick the category for you."
        )
        return

    amount, note = parsed

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await _resolve_account(message, session, user.id)
            if account is None:
                return

            await finance_service.add_transaction(
                session,
                account.id,
                type=transaction_type,
                amount=amount,
                category="Other",
                note=note,
            )

            tz_name = await _get_user_timezone_name(session, account.id)
            tz = _user_zone(tz_name)
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

        icon = "💸" if transaction_type == "expense" else "💰"
        label = "Expense" if transaction_type == "expense" else "Income"

        text = (
            f"{icon} {label} added: "
            f"{finance_service.format_money(amount)} BDT"
        )
        if note:
            text += f" — {note}"
        text += (
            f"\nToday: spent "
            f"{finance_service.format_money(summary['total_expense'])} · "
            f"earned "
            f"{finance_service.format_money(summary['total_income'])} (BDT)"
        )

        await message.reply_text(text)

    except Exception:
        logger.exception(
            "Unable to quick-add %s for user %s",
            transaction_type,
            user.id,
        )

        await message.reply_text(
            "I couldn't record that. Please try again."
        )


async def expense_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _quick_add_command(update, context, transaction_type="expense")


async def income_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _quick_add_command(update, context, transaction_type="income")


def _period_label(period: str, now_local: datetime) -> str:
    if period == "today":
        return f"Today ({now_local.strftime('%a %d %b')})"
    if period == "week":
        return "This week"
    if period == "month":
        return f"{now_local.strftime('%B %Y')}"
    if period == "last_month":
        previous = now_local.replace(day=1) - timedelta(days=1)
        return f"{previous.strftime('%B %Y')}"
    return period


async def summary_command(
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
            "Please use /summary in a private conversation."
        )
        return

    args = context.args or []
    period = args[0].strip().lower() if args else None

    if period is not None and period not in (
        "today",
        "yesterday",
        "week",
        "month",
        "last_month",
    ):
        await message.reply_text(
            "Usage: /summary [today|week|month|last_month]\n"
            "With no argument, I'll show yesterday's overview."
        )
        return

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await _resolve_account(message, session, user.id)
            if account is None:
                return

            tz_name = await _get_user_timezone_name(session, account.id)
            tz = _user_zone(tz_name)
            now = datetime.now(timezone.utc)

            # No argument (or "yesterday"): the morning overview
            # of yesterday's money.
            if period is None or period == "yesterday":
                text = await finance_service.build_yesterday_overview(
                    session,
                    account.id,
                    tz,
                    now,
                )
                await _send_long_message(message, text)
                return

            since, until = finance_service.period_range(period, tz, now)
            summary = await finance_service.summarize(
                session,
                account.id,
                since=since,
                until=until,
            )
            totals = await finance_service.debt_totals(
                session,
                account.id,
            )

        fmt = finance_service.format_money
        now_local = now.astimezone(tz)
        label = _period_label(period, now_local)

        lines = [
            f"📊 Summary — {label}",
            f"\n💰 Income: {fmt(summary['total_income'])} BDT",
            f"💸 Expenses: {fmt(summary['total_expense'])} BDT",
        ]

        balance = summary["balance"]
        sign = "+" if balance > 0 else ""
        lines.append(f"Net: {sign}{fmt(balance)} BDT")

        if summary["expense_by_category"]:
            top = " · ".join(
                f"{item['category']} {fmt(item['total'])}"
                for item in summary["expense_by_category"][:5]
            )
            lines.append(f"\nTop expenses: {top}")

        if summary["income_by_category"]:
            top_income = " · ".join(
                f"{item['category']} {fmt(item['total'])}"
                for item in summary["income_by_category"][:5]
            )
            lines.append(f"Top income: {top_income}")

        if totals["open_count"]:
            lines.append(
                f"\nOpen debts: you owe {fmt(totals['owed_by_user'])} · "
                f"owed to you {fmt(totals['owed_to_user'])} (BDT)"
            )

        if summary["transaction_count"] == 0:
            lines.append("\nNo transactions recorded in this period.")

        await _send_long_message(message, "\n".join(lines))

    except Exception:
        logger.exception(
            "Unable to build summary for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't build your summary. Please try again."
        )


async def debts_command(
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
            "Please use /debts in a private conversation."
        )
        return

    try:
        session_factory = get_session_factory()

        async with session_factory() as session:
            account = await _resolve_account(message, session, user.id)
            if account is None:
                return

            debts = await finance_service.list_debts(session, account.id)
            totals = await finance_service.debt_totals(session, account.id)

        if not debts:
            await message.reply_text(
                "You have no open debts or loans. 🎉\n\n"
                "Tell me in chat when you borrow or lend money, "
                "like \"borrowed 5000 from Rahim\", and I'll track it."
            )
            return

        fmt = finance_service.format_money
        lines = ["🤝 Your open debts & loans"]

        borrowed = [d for d in debts if d.direction == "borrowed"]
        lent = [d for d in debts if d.direction == "lent"]

        if borrowed:
            lines.append("\nYou owe:")
            for debt in borrowed:
                lines.append(
                    f"• {debt.counterparty} — {fmt(debt.remaining)} BDT "
                    f"left (of {fmt(debt.principal)}) "
                    f"(id: {str(debt.id)[:8]})"
                )

        if lent:
            lines.append("\nOwed to you:")
            for debt in lent:
                lines.append(
                    f"• {debt.counterparty} — {fmt(debt.remaining)} BDT "
                    f"left (of {fmt(debt.principal)}) "
                    f"(id: {str(debt.id)[:8]})"
                )

        lines.append(
            f"\nTotals: you owe {fmt(totals['owed_by_user'])} · "
            f"owed to you {fmt(totals['owed_to_user'])} (BDT)"
        )
        lines.append(
            "\nTo record a repayment, tell me in chat, "
            "e.g. \"Rahim paid back 1000\"."
        )

        await _send_long_message(message, "\n".join(lines))

    except Exception:
        logger.exception(
            "Unable to list debts for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't load your debts. Please try again."
        )
