import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Debt, DebtPayment, Reminder, Transaction, User
from app.services import reminder_service

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "BDT"

# The category vocabularies. Gemini picks one from the context of
# the user's message; the backend normalizes whatever it returns
# against these sets so summaries stay consistent.
EXPENSE_CATEGORIES = [
    "Food",
    "Transport",
    "Shopping",
    "Bills",
    "Rent",
    "Health",
    "Entertainment",
    "Education",
    "Family",
    "Other",
]

INCOME_CATEGORIES = [
    "Salary",
    "Business",
    "Freelance",
    "Gift",
    "Other",
]

TRANSACTION_TYPES = ("income", "expense")
DEBT_DIRECTIONS = ("borrowed", "lent")
DEBT_KINDS = ("debt", "loan")

MAX_NOTE_LENGTH = 1000
MAX_COUNTERPARTY_LENGTH = 120


def normalize_category(transaction_type: str, raw) -> str:
    """
    Map a free-form category onto the canonical set for the type.

    Matching is case-insensitive and ignores surrounding whitespace;
    anything unrecognized (or not a string) becomes "Other".
    """
    categories = (
        INCOME_CATEGORIES
        if transaction_type == "income"
        else EXPENSE_CATEGORIES
    )

    if isinstance(raw, str):
        wanted = raw.strip().lower()
        for category in categories:
            if category.lower() == wanted:
                return category

    return "Other"


def parse_amount(value) -> Decimal:
    """
    Convert a user/model supplied amount to a positive Decimal.

    Accepts ints, floats, Decimals and numeric strings (commas are
    ignored, so "45,000" works). Raises ValueError for anything
    else, for non-finite values, and for amounts <= 0.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError("amount must be a positive number.")

    try:
        if isinstance(value, str):
            amount = Decimal(value.strip().replace(",", ""))
        else:
            amount = Decimal(str(value))

        if not amount.is_finite() or amount <= 0:
            raise ValueError("amount must be a positive number.")

        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("amount must be a positive number.") from None


def format_money(value) -> str:
    """Render an amount compactly: 45000 -> "45,000", 1250.5 -> "1,250.50"."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"))

    if amount == amount.to_integral_value():
        return f"{int(amount):,}"

    return f"{amount:,.2f}"


# --- periods ----------------------------------------------------------


def period_range(
    name: str,
    tz: ZoneInfo,
    now: datetime,
) -> tuple[datetime, datetime]:
    """
    Return the [since, until) bounds of a named period in the
    user's timezone, as aware local datetimes.

    Supported names: "today", "yesterday", "week" (Monday to
    Sunday of the current week), "month" (the current calendar
    month) and "last_month". `now` may be in any timezone; it is
    converted into `tz` first.
    """
    local = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    today = local.date()

    if name == "today":
        start = today
        end = today + timedelta(days=1)
    elif name == "yesterday":
        start = today - timedelta(days=1)
        end = today
    elif name == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif name == "month":
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    elif name == "last_month":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    else:
        raise ValueError(
            "period must be one of: today, yesterday, week, month, "
            "last_month."
        )

    return (
        datetime.combine(start, time.min, tzinfo=tz),
        datetime.combine(end, time.min, tzinfo=tz),
    )


# --- transactions -----------------------------------------------------


async def add_transaction(
    session: AsyncSession,
    user_id: UUID,
    *,
    type: str,
    amount,
    category,
    note: str | None = None,
    occurred_at: datetime | None = None,
    currency: str = DEFAULT_CURRENCY,
) -> Transaction:
    """Store one income/expense transaction for a user."""
    if type not in TRANSACTION_TYPES:
        raise ValueError("type must be 'income' or 'expense'.")

    amount = parse_amount(amount)

    if occurred_at is None:
        occurred_at = datetime.now(timezone.utc)
    elif occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    transaction = Transaction(
        user_id=user_id,
        type=type,
        amount=amount,
        currency=(currency or DEFAULT_CURRENCY).strip().upper()[:3]
        or DEFAULT_CURRENCY,
        category=normalize_category(type, category),
        note=(note.strip()[:MAX_NOTE_LENGTH] or None)
        if isinstance(note, str)
        else None,
        occurred_at=occurred_at,
    )
    session.add(transaction)
    await session.commit()
    return transaction


async def list_transactions(
    session: AsyncSession,
    user_id: UUID,
    *,
    since: datetime,
    until: datetime,
    type: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[Transaction]:
    """Return a user's transactions in [since, until), newest first."""
    query = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.occurred_at >= since,
            Transaction.occurred_at < until,
        )
        .order_by(
            Transaction.occurred_at.desc(),
            Transaction.id.desc(),
        )
        .limit(limit)
    )

    if type is not None:
        query = query.where(Transaction.type == type)
    if category is not None:
        query = query.where(Transaction.category == category)

    return list(await session.scalars(query))


def aggregate_transactions(rows) -> dict:
    """
    Pure aggregation over transaction rows (Decimals preserved).

    Split out from summarize() so the math can be unit-tested
    without a database.
    """
    total_income = Decimal("0")
    total_expense = Decimal("0")
    expense_totals: dict[str, Decimal] = {}
    income_totals: dict[str, Decimal] = {}

    for row in rows:
        amount = Decimal(str(row.amount))
        if row.type == "income":
            total_income += amount
            income_totals[row.category] = (
                income_totals.get(row.category, Decimal("0")) + amount
            )
        else:
            total_expense += amount
            expense_totals[row.category] = (
                expense_totals.get(row.category, Decimal("0")) + amount
            )

    def ranked(totals: dict[str, Decimal]) -> list[dict]:
        ordered = sorted(
            totals.items(),
            key=lambda item: (-item[1], item[0]),
        )
        return [
            {"category": category, "total": total}
            for category, total in ordered
        ]

    return {
        "total_income": total_income,
        "total_expense": total_expense,
        "balance": total_income - total_expense,
        "expense_by_category": ranked(expense_totals),
        "income_by_category": ranked(income_totals),
        "transaction_count": len(rows),
    }


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


async def summarize(
    session: AsyncSession,
    user_id: UUID,
    *,
    since: datetime,
    until: datetime,
) -> dict:
    """
    Summarize a user's transactions in [since, until).

    Amounts are returned as floats rounded to 2 decimals so the
    result is JSON-friendly for AI tool responses.
    """
    rows = list(
        await session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.occurred_at >= since,
                Transaction.occurred_at < until,
            )
        )
    )

    raw = aggregate_transactions(rows)

    return {
        "total_income": _money(raw["total_income"]),
        "total_expense": _money(raw["total_expense"]),
        "balance": _money(raw["balance"]),
        "expense_by_category": [
            {"category": item["category"], "total": _money(item["total"])}
            for item in raw["expense_by_category"]
        ],
        "income_by_category": [
            {"category": item["category"], "total": _money(item["total"])}
            for item in raw["income_by_category"]
        ],
        "transaction_count": raw["transaction_count"],
    }


async def last_transaction_time(
    session: AsyncSession,
    user_id: UUID,
) -> datetime | None:
    """Return when the user last logged a transaction (or None)."""
    return await session.scalar(
        select(func.max(Transaction.created_at)).where(
            Transaction.user_id == user_id
        )
    )


# --- debts ------------------------------------------------------------


async def add_debt(
    session: AsyncSession,
    user_id: UUID,
    *,
    direction: str,
    kind: str = "debt",
    counterparty: str,
    amount,
    note: str | None = None,
    due_date: date | None = None,
) -> Debt:
    """
    Open a debt record for a user.

    direction "borrowed" means the user owes the counterparty;
    "lent" means the counterparty owes the user.
    """
    if direction not in DEBT_DIRECTIONS:
        raise ValueError("direction must be 'borrowed' or 'lent'.")

    if not isinstance(kind, str) or kind.strip().lower() not in DEBT_KINDS:
        raise ValueError("kind must be 'debt' or 'loan'.")

    if not isinstance(counterparty, str) or not counterparty.strip():
        raise ValueError("A counterparty name is required.")

    principal = parse_amount(amount)

    debt = Debt(
        user_id=user_id,
        direction=direction,
        kind=kind.strip().lower(),
        counterparty=counterparty.strip()[:MAX_COUNTERPARTY_LENGTH],
        principal=principal,
        remaining=principal,
        note=(note.strip()[:MAX_NOTE_LENGTH] or None)
        if isinstance(note, str)
        else None,
        due_date=due_date,
        status="open",
    )
    session.add(debt)
    await session.commit()
    return debt


async def record_debt_payment(
    session: AsyncSession,
    user_id: UUID,
    debt_id: UUID,
    amount,
) -> Debt:
    """
    Record a payment against one of the user's debts.

    Writes a DebtPayment ledger row and reduces the remaining
    balance; a payment that clears the balance settles the debt.
    Payments larger than the remaining balance are rejected so the
    ledger can never go negative.
    """
    payment_amount = parse_amount(amount)

    debt = await session.scalar(
        select(Debt).where(
            Debt.id == debt_id,
            Debt.user_id == user_id,
        )
    )

    if debt is None:
        raise ValueError("That debt was not found among your debts.")

    if debt.status != "open":
        raise ValueError("That debt is already settled.")

    remaining = Decimal(str(debt.remaining))

    if payment_amount > remaining:
        raise ValueError(
            f"Payment of {format_money(payment_amount)} exceeds the "
            f"remaining balance of {format_money(remaining)}."
        )

    session.add(
        DebtPayment(
            debt_id=debt.id,
            user_id=user_id,
            amount=payment_amount,
            paid_at=datetime.now(timezone.utc),
        )
    )

    debt.remaining = remaining - payment_amount
    if debt.remaining <= 0:
        debt.remaining = Decimal("0")
        debt.status = "settled"

    await session.commit()
    return debt


async def list_debts(
    session: AsyncSession,
    user_id: UUID,
    *,
    status: str = "open",
    direction: str | None = None,
) -> list[Debt]:
    """Return a user's debts, oldest first (stable short IDs)."""
    query = (
        select(Debt)
        .where(
            Debt.user_id == user_id,
            Debt.status == status,
        )
        .order_by(Debt.created_at.asc(), Debt.id.asc())
    )

    if direction is not None:
        query = query.where(Debt.direction == direction)

    return list(await session.scalars(query))


async def debt_totals(
    session: AsyncSession,
    user_id: UUID,
) -> dict:
    """Open-debt totals: what the user owes and is owed."""
    owed_by_user = Decimal("0")
    owed_to_user = Decimal("0")
    open_count = 0

    for debt in await list_debts(session, user_id, status="open"):
        open_count += 1
        if debt.direction == "borrowed":
            owed_by_user += Decimal(str(debt.remaining))
        else:
            owed_to_user += Decimal(str(debt.remaining))

    return {
        "owed_by_user": _money(owed_by_user),
        "owed_to_user": _money(owed_to_user),
        "open_count": open_count,
    }


# --- expense check-in nudges ------------------------------------------
#
# Nudges and the morning overview are ordinary recurring daily
# reminders with a recognizable title and a special delivery
# job_type, so they ride the existing scheduler and delivery
# worker. They are identified by (user, title, status "active").

EXPENSE_NUDGE_TITLE = "🧾 Expense check-in"
EXPENSE_NUDGE_DESCRIPTION = (
    "Log what you spent or earned since last time — "
    "e.g. 'lunch 250' or 'salary 45000'."
)
EXPENSE_NUDGE_JOB_TYPE = "expense_nudge"

DAILY_OVERVIEW_TITLE = "🧾 Yesterday's money overview"
DAILY_OVERVIEW_JOB_TYPE = "daily_overview"
DAILY_OVERVIEW_DEFAULT_TIME = "08:00"

# How the check-in times spread across the day per frequency.
NUDGE_TIME_SLOTS: dict[int, list[str]] = {
    1: ["21:00"],
    2: ["13:00", "21:00"],
    3: ["10:00", "14:00", "21:00"],
    4: ["09:00", "13:00", "18:00", "22:00"],
    5: ["09:00", "12:00", "15:00", "18:00", "21:30"],
    6: ["08:30", "11:00", "13:30", "16:00", "18:30", "21:30"],
    7: ["08:00", "10:30", "13:00", "15:30", "18:00", "20:00", "22:00"],
    8: ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"],
}


def parse_hhmm(value) -> tuple[int, int]:
    """Parse an "HH:MM" string into (hour, minute); ValueError if bad."""
    if not isinstance(value, str):
        raise ValueError("time must be in HH:MM format (e.g. 08:00).")

    parts = value.strip().split(":")

    if len(parts) != 2:
        raise ValueError("time must be in HH:MM format (e.g. 08:00).")

    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(
            "time must be in HH:MM format (e.g. 08:00)."
        ) from None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time must be in HH:MM format (e.g. 08:00).")

    return hour, minute


def nudge_slots(times_per_day: int) -> list[tuple[int, int]]:
    """Return the (hour, minute) check-in slots for a frequency."""
    if times_per_day not in NUDGE_TIME_SLOTS:
        raise ValueError("times_per_day must be between 1 and 8.")

    return [parse_hhmm(slot) for slot in NUDGE_TIME_SLOTS[times_per_day]]


def next_daily_run(
    hour: int,
    minute: int,
    tz: ZoneInfo,
    now: datetime,
) -> datetime:
    """The next local occurrence of a daily HH:MM slot, strictly future."""
    now_local = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)

    candidate = now_local.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    if candidate <= now_local:
        candidate += timedelta(days=1)

    return candidate


async def _cancel_active_by_title(
    session: AsyncSession,
    user_id: UUID,
    title: str,
) -> int:
    """Cancel every active reminder of the user's with this title."""
    reminders = list(
        await session.scalars(
            select(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.title == title,
                Reminder.status == "active",
            )
        )
    )

    cancelled = 0
    for reminder in reminders:
        if await reminder_service.cancel_reminder(
            session,
            user_id,
            reminder.id,
        ):
            cancelled += 1

    return cancelled


async def set_expense_nudges(
    session: AsyncSession,
    user: User,
    *,
    enabled: bool,
    times_per_day: int = 4,
    timezone_name: str,
    now: datetime | None = None,
) -> dict:
    """
    Turn the daily expense check-in nudges on or off for a user.

    Enabling replaces any existing nudges with a fresh set of
    daily reminders at the spread times for the chosen frequency;
    disabling cancels them all.
    """
    await _cancel_active_by_title(session, user.id, EXPENSE_NUDGE_TITLE)

    if not enabled:
        return {"enabled": False, "times_per_day": 0, "times": []}

    slots = nudge_slots(times_per_day)
    tz = ZoneInfo(timezone_name)
    now = now or datetime.now(timezone.utc)

    times = []
    for hour, minute in slots:
        first_run = next_daily_run(hour, minute, tz, now)
        await reminder_service.create_reminder(
            session,
            user,
            title=EXPENSE_NUDGE_TITLE,
            description=EXPENSE_NUDGE_DESCRIPTION,
            first_run_at=first_run,
            recurrence="daily",
            timezone_name=timezone_name,
            job_type=EXPENSE_NUDGE_JOB_TYPE,
        )
        times.append(f"{hour:02d}:{minute:02d}")

    return {
        "enabled": True,
        "times_per_day": times_per_day,
        "times": times,
    }


async def set_daily_overview(
    session: AsyncSession,
    user: User,
    *,
    enabled: bool,
    time_str: str | None = None,
    timezone_name: str,
    now: datetime | None = None,
) -> dict:
    """
    Turn the morning "yesterday's money" overview on or off.

    Enabling replaces any existing overview reminder with one
    daily reminder at the requested local time (default 08:00);
    the delivery worker renders the overview text at send time.
    """
    await _cancel_active_by_title(session, user.id, DAILY_OVERVIEW_TITLE)

    time_str = (time_str or DAILY_OVERVIEW_DEFAULT_TIME).strip()
    hour, minute = parse_hhmm(time_str)

    if not enabled:
        return {"enabled": False, "time": time_str}

    tz = ZoneInfo(timezone_name)
    now = now or datetime.now(timezone.utc)

    await reminder_service.create_reminder(
        session,
        user,
        title=DAILY_OVERVIEW_TITLE,
        first_run_at=next_daily_run(hour, minute, tz, now),
        recurrence="daily",
        timezone_name=timezone_name,
        job_type=DAILY_OVERVIEW_JOB_TYPE,
    )

    return {"enabled": True, "time": time_str}


# --- day overview (morning report of yesterday's money) ---------------


def format_yesterday_overview(
    *,
    day_word: str = "Yesterday",
    date_label: str,
    expense_total,
    expense_by_category,
    income_total,
    income_by_category,
    largest_expense=None,
    borrowed_new=(),
    lent_new=(),
    payments_made=(),
    payments_received=(),
    owed_by_user=0,
    owed_to_user=0,
    currency: str = DEFAULT_CURRENCY,
) -> str:
    """
    Render one day's money activity as compact Telegram text.

    Pure formatter: category inputs are (name, amount) pairs in
    display order (only the top 5 are shown); debt activity inputs
    are (counterparty, amount) pairs. Sections with nothing in
    them are omitted.
    """
    lines = [f"📊 {day_word} — {date_label}"]

    has_activity = bool(
        expense_by_category
        or income_by_category
        or borrowed_new
        or lent_new
        or payments_made
        or payments_received
    )

    if not has_activity:
        lines.append(
            f"\nNo money activity recorded {day_word.lower()}."
        )
    else:
        if expense_by_category or Decimal(str(expense_total)) > 0:
            lines.append(
                f"\n💸 Expenses: {format_money(expense_total)} {currency}"
            )
            for category, amount in list(expense_by_category)[:5]:
                lines.append(f"• {category} — {format_money(amount)}")
            if largest_expense is not None:
                label, amount = largest_expense
                lines.append(
                    f"Largest: {label} — {format_money(amount)}"
                )

        if income_by_category or Decimal(str(income_total)) > 0:
            lines.append(
                f"\n💰 Income: {format_money(income_total)} {currency}"
            )
            for category, amount in list(income_by_category)[:5]:
                lines.append(f"• {category} — {format_money(amount)}")

        activity_lines = []
        for counterparty, amount in borrowed_new:
            activity_lines.append(
                f"• Borrowed {format_money(amount)} from {counterparty}"
            )
        for counterparty, amount in lent_new:
            activity_lines.append(
                f"• Lent {format_money(amount)} to {counterparty}"
            )
        for counterparty, amount in payments_made:
            activity_lines.append(
                f"• Paid back {format_money(amount)} to {counterparty}"
            )
        for counterparty, amount in payments_received:
            activity_lines.append(
                f"• Received {format_money(amount)} from {counterparty}"
            )

        if activity_lines:
            lines.append("\n🤝 Loans & debts")
            lines.extend(activity_lines)

    if Decimal(str(owed_by_user)) > 0 or Decimal(str(owed_to_user)) > 0:
        lines.append(
            f"\nOpen debts: you owe {format_money(owed_by_user)} · "
            f"owed to you {format_money(owed_to_user)} ({currency})"
        )

    return "\n".join(lines)


async def build_day_overview(
    session: AsyncSession,
    user_id: UUID,
    tz: ZoneInfo,
    now: datetime,
    *,
    day: str = "yesterday",
) -> str:
    """Gather one day's activity and render its overview text."""
    since, until = period_range(day, tz, now)

    summary = await summarize(session, user_id, since=since, until=until)

    largest = await session.scalar(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.occurred_at >= since,
            Transaction.occurred_at < until,
        )
        .order_by(Transaction.amount.desc(), Transaction.id.desc())
        .limit(1)
    )

    new_debts = list(
        await session.scalars(
            select(Debt)
            .where(
                Debt.user_id == user_id,
                Debt.created_at >= since,
                Debt.created_at < until,
            )
            .order_by(Debt.created_at.asc(), Debt.id.asc())
        )
    )

    payment_rows = (
        await session.execute(
            select(DebtPayment, Debt)
            .join(Debt, Debt.id == DebtPayment.debt_id)
            .where(
                DebtPayment.user_id == user_id,
                DebtPayment.paid_at >= since,
                DebtPayment.paid_at < until,
            )
            .order_by(DebtPayment.paid_at.asc())
        )
    ).all()

    totals = await debt_totals(session, user_id)

    return format_yesterday_overview(
        day_word="Yesterday" if day == "yesterday" else "Today",
        date_label=since.astimezone(tz).strftime("%a %d %b %Y"),
        expense_total=summary["total_expense"],
        expense_by_category=[
            (item["category"], item["total"])
            for item in summary["expense_by_category"]
        ],
        income_total=summary["total_income"],
        income_by_category=[
            (item["category"], item["total"])
            for item in summary["income_by_category"]
        ],
        largest_expense=(
            (
                largest.note or largest.category,
                float(largest.amount),
            )
            if largest is not None
            else None
        ),
        borrowed_new=[
            (debt.counterparty, float(debt.principal))
            for debt in new_debts
            if debt.direction == "borrowed"
        ],
        lent_new=[
            (debt.counterparty, float(debt.principal))
            for debt in new_debts
            if debt.direction == "lent"
        ],
        payments_made=[
            (debt.counterparty, float(payment.amount))
            for payment, debt in payment_rows
            if debt.direction == "borrowed"
        ],
        payments_received=[
            (debt.counterparty, float(payment.amount))
            for payment, debt in payment_rows
            if debt.direction == "lent"
        ],
        owed_by_user=totals["owed_by_user"],
        owed_to_user=totals["owed_to_user"],
    )


async def build_yesterday_overview(
    session: AsyncSession,
    user_id: UUID,
    tz: ZoneInfo,
    now: datetime,
) -> str:
    """Render yesterday's money overview for a user."""
    return await build_day_overview(
        session,
        user_id,
        tz,
        now,
        day="yesterday",
    )
