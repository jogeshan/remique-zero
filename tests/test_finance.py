"""Tests for the expense tracker (transactions, debts, nudges,
daily overview).

Covers the pure helpers (category normalization, amount parsing,
period ranges, aggregation, the overview formatter, the nudge
time table), service-level debt payment math with a fake session
(including the DebtPayment ledger row), the AI tool dispatch for
the finance tools, and the delivery worker's special handling of
expense_nudge and daily_overview jobs. Real-DB behaviour is left
to the integration suite (aiosqlite is not available in the test
environment).
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.models import Debt, DebtPayment, DeliveryJob, Reminder, ReminderOccurrence
from app.services import ai_tools, delivery_service, finance_service
from app.services import memory_service
from app.services.ai_service import MUTATING_TOOLS, build_system_instruction
from app.services.finance_service import (
    aggregate_transactions,
    format_money,
    format_yesterday_overview,
    normalize_category,
    nudge_slots,
    parse_amount,
    parse_hhmm,
    period_range,
    next_daily_run,
)

DHAKA = ZoneInfo("Asia/Dhaka")
# 2026-11-15 is a Sunday.
NOW_LOCAL = datetime(2026, 11, 15, 10, 0, tzinfo=DHAKA)


# --- normalize_category -----------------------------------------------


def test_normalize_category_matches_case_insensitively():
    assert normalize_category("expense", "food") == "Food"
    assert normalize_category("expense", " TRANSPORT ") == "Transport"
    assert normalize_category("income", "salary") == "Salary"
    assert normalize_category("income", "Freelance") == "Freelance"


def test_normalize_category_falls_back_to_other():
    assert normalize_category("expense", "spaceships") == "Other"
    assert normalize_category("expense", None) == "Other"
    assert normalize_category("expense", "") == "Other"
    # "Salary" is not an expense category.
    assert normalize_category("expense", "Salary") == "Other"
    # "Rent" is not an income category.
    assert normalize_category("income", "Rent") == "Other"


# --- parse_amount / format_money ---------------------------------------


def test_parse_amount_accepts_numbers_and_strings():
    assert parse_amount(250) == Decimal("250.00")
    assert parse_amount(1250.5) == Decimal("1250.50")
    assert parse_amount("45,000") == Decimal("45000.00")
    assert parse_amount(Decimal("99.9")) == Decimal("99.90")


def test_parse_amount_rejects_bad_values():
    for bad in (0, -5, "abc", "", None, True, "1e999999"):
        with pytest.raises(ValueError):
            parse_amount(bad)


def test_format_money():
    assert format_money(45000) == "45,000"
    assert format_money(Decimal("1250.50")) == "1,250.50"
    assert format_money(0) == "0"
    assert format_money(-3200) == "-3,200"


# --- period_range -------------------------------------------------------


def test_period_range_today_and_yesterday():
    since, until = period_range("today", DHAKA, NOW_LOCAL)
    assert since == datetime(2026, 11, 15, 0, 0, tzinfo=DHAKA)
    assert until == datetime(2026, 11, 16, 0, 0, tzinfo=DHAKA)

    since, until = period_range("yesterday", DHAKA, NOW_LOCAL)
    assert since == datetime(2026, 11, 14, 0, 0, tzinfo=DHAKA)
    assert until == datetime(2026, 11, 15, 0, 0, tzinfo=DHAKA)


def test_period_range_week_starts_on_monday():
    # NOW_LOCAL is a Sunday, so the week runs Mon 9th - Mon 16th.
    since, until = period_range("week", DHAKA, NOW_LOCAL)
    assert since == datetime(2026, 11, 9, 0, 0, tzinfo=DHAKA)
    assert until == datetime(2026, 11, 16, 0, 0, tzinfo=DHAKA)


def test_period_range_month_and_last_month():
    since, until = period_range("month", DHAKA, NOW_LOCAL)
    assert since == datetime(2026, 11, 1, 0, 0, tzinfo=DHAKA)
    assert until == datetime(2026, 12, 1, 0, 0, tzinfo=DHAKA)

    since, until = period_range("last_month", DHAKA, NOW_LOCAL)
    assert since == datetime(2026, 10, 1, 0, 0, tzinfo=DHAKA)
    assert until == datetime(2026, 11, 1, 0, 0, tzinfo=DHAKA)


def test_period_range_rejects_unknown_period():
    with pytest.raises(ValueError):
        period_range("year", DHAKA, NOW_LOCAL)


# --- aggregate_transactions ----------------------------------------------


def _txn(type_, amount, category):
    return SimpleNamespace(
        type=type_,
        amount=Decimal(amount),
        category=category,
    )


def test_aggregate_transactions_totals_and_ordering():
    rows = [
        _txn("expense", "200", "Transport"),
        _txn("expense", "1200", "Food"),
        _txn("expense", "300", "Food"),
        _txn("income", "45000", "Salary"),
        _txn("income", "500", "Gift"),
    ]

    result = aggregate_transactions(rows)

    assert result["total_income"] == Decimal("45500")
    assert result["total_expense"] == Decimal("1700")
    assert result["balance"] == Decimal("43800")
    assert result["transaction_count"] == 5
    # Categories ranked by total, descending.
    assert result["expense_by_category"] == [
        {"category": "Food", "total": Decimal("1500")},
        {"category": "Transport", "total": Decimal("200")},
    ]
    assert result["income_by_category"][0] == {
        "category": "Salary",
        "total": Decimal("45000"),
    }


def test_aggregate_transactions_empty():
    result = aggregate_transactions([])

    assert result["total_income"] == Decimal("0")
    assert result["total_expense"] == Decimal("0")
    assert result["balance"] == Decimal("0")
    assert result["expense_by_category"] == []
    assert result["transaction_count"] == 0


# --- add_transaction validation (fake session) ---------------------------


class _RecordingSession:
    """Fake session that records added objects."""

    def __init__(self, scalar_result=None):
        self.added = []
        self.scalar_result = scalar_result
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def scalar(self, *args, **kwargs):
        return self.scalar_result

    async def scalars(self, *args, **kwargs):
        return []

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


async def test_add_transaction_rejects_non_positive_amount():
    session = _RecordingSession()
    user_id = uuid.uuid4()

    for bad_amount in (0, -100):
        with pytest.raises(ValueError):
            await finance_service.add_transaction(
                session,
                user_id,
                type="expense",
                amount=bad_amount,
                category="Food",
            )

    assert session.added == []


async def test_add_transaction_rejects_bad_type():
    session = _RecordingSession()

    with pytest.raises(ValueError):
        await finance_service.add_transaction(
            session,
            uuid.uuid4(),
            type="transfer",
            amount=100,
            category="Food",
        )


async def test_add_transaction_normalizes_and_defaults():
    session = _RecordingSession()
    user_id = uuid.uuid4()

    txn = await finance_service.add_transaction(
        session,
        user_id,
        type="expense",
        amount="250",
        category="food",
        note="  lunch  ",
    )

    assert txn.user_id == user_id
    assert txn.amount == Decimal("250.00")
    assert txn.category == "Food"  # normalized
    assert txn.currency == "BDT"
    assert txn.note == "lunch"
    assert txn.occurred_at is not None
    assert session.committed is True


# --- debt payments --------------------------------------------------------


def _debt(remaining="5000", principal="5000", status="open"):
    return Debt(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        direction="borrowed",
        kind="debt",
        counterparty="Rahim",
        principal=Decimal(principal),
        remaining=Decimal(remaining),
        status=status,
    )


async def test_record_debt_payment_partial_writes_ledger_row():
    debt = _debt()
    session = _RecordingSession(scalar_result=debt)

    result = await finance_service.record_debt_payment(
        session,
        debt.user_id,
        debt.id,
        1000,
    )

    assert result is debt
    assert debt.remaining == Decimal("4000")
    assert debt.status == "open"

    payments = [obj for obj in session.added if isinstance(obj, DebtPayment)]
    assert len(payments) == 1
    assert payments[0].debt_id == debt.id
    assert payments[0].user_id == debt.user_id
    assert payments[0].amount == Decimal("1000.00")
    assert payments[0].paid_at is not None


async def test_record_debt_payment_full_amount_settles():
    debt = _debt(remaining="1500")
    session = _RecordingSession(scalar_result=debt)

    await finance_service.record_debt_payment(
        session,
        debt.user_id,
        debt.id,
        "1500",
    )

    assert debt.remaining == Decimal("0")
    assert debt.status == "settled"


async def test_record_debt_payment_rejects_overpayment():
    debt = _debt(remaining="500")
    session = _RecordingSession(scalar_result=debt)

    with pytest.raises(ValueError) as excinfo:
        await finance_service.record_debt_payment(
            session,
            debt.user_id,
            debt.id,
            501,
        )

    assert "exceeds the remaining balance" in str(excinfo.value)
    assert debt.remaining == Decimal("500")
    assert debt.status == "open"
    assert [
        obj for obj in session.added if isinstance(obj, DebtPayment)
    ] == []


async def test_record_debt_payment_rejects_settled_and_missing():
    settled = _debt(status="settled")
    session = _RecordingSession(scalar_result=settled)

    with pytest.raises(ValueError):
        await finance_service.record_debt_payment(
            session,
            settled.user_id,
            settled.id,
            100,
        )

    missing = _RecordingSession(scalar_result=None)
    with pytest.raises(ValueError):
        await finance_service.record_debt_payment(
            missing,
            uuid.uuid4(),
            uuid.uuid4(),
            100,
        )


async def test_add_debt_validation():
    session = _RecordingSession()

    with pytest.raises(ValueError):
        await finance_service.add_debt(
            session,
            uuid.uuid4(),
            direction="sideways",
            counterparty="Rahim",
            amount=100,
        )

    with pytest.raises(ValueError):
        await finance_service.add_debt(
            session,
            uuid.uuid4(),
            direction="borrowed",
            counterparty="  ",
            amount=100,
        )

    debt = await finance_service.add_debt(
        session,
        uuid.uuid4(),
        direction="lent",
        kind="loan",
        counterparty="Karim",
        amount=2000,
    )
    assert debt.remaining == debt.principal == Decimal("2000.00")
    assert debt.status == "open"


# --- nudge schedule helpers -----------------------------------------------


def test_nudge_slots_table():
    assert nudge_slots(1) == [(21, 0)]
    assert nudge_slots(2) == [(13, 0), (21, 0)]
    assert nudge_slots(3) == [(10, 0), (14, 0), (21, 0)]
    assert nudge_slots(4) == [(9, 0), (13, 0), (18, 0), (22, 0)]
    assert nudge_slots(5) == [(9, 0), (12, 0), (15, 0), (18, 0), (21, 30)]
    assert nudge_slots(6) == [
        (8, 30), (11, 0), (13, 30), (16, 0), (18, 30), (21, 30),
    ]
    assert len(nudge_slots(7)) == 7
    assert len(nudge_slots(8)) == 8
    assert nudge_slots(8)[-1] == (22, 0)


def test_nudge_slots_rejects_out_of_range():
    for bad in (0, 9, -1):
        with pytest.raises(ValueError):
            nudge_slots(bad)


def test_parse_hhmm():
    assert parse_hhmm("08:00") == (8, 0)
    assert parse_hhmm("21:30") == (21, 30)
    for bad in ("8", "25:00", "08:60", "morning", None, 8):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_next_daily_run():
    # 10:00 local: today's 21:00 slot is still ahead.
    run = next_daily_run(21, 0, DHAKA, NOW_LOCAL)
    assert run == datetime(2026, 11, 15, 21, 0, tzinfo=DHAKA)

    # The 09:00 slot has passed, so it lands tomorrow.
    run = next_daily_run(9, 0, DHAKA, NOW_LOCAL)
    assert run == datetime(2026, 11, 16, 9, 0, tzinfo=DHAKA)


# --- format_yesterday_overview ---------------------------------------------


def test_format_yesterday_overview_full():
    text = format_yesterday_overview(
        date_label="Sat 14 Nov 2026",
        expense_total=1850,
        expense_by_category=[("Food", 1200), ("Transport", 650)],
        income_total=45000,
        income_by_category=[("Salary", 45000)],
        largest_expense=("Lunch at Sultan's Dine", 850),
        borrowed_new=[("Rahim", 5000)],
        lent_new=[("Karim", 2000)],
        payments_made=[("Rahim", 1000)],
        payments_received=[("Karim", 500)],
        owed_by_user=9000,
        owed_to_user=3500,
    )

    assert text.startswith("📊 Yesterday — Sat 14 Nov 2026")
    assert "💸 Expenses: 1,850 BDT" in text
    # Category order preserved as given (largest first).
    assert text.index("• Food — 1,200") < text.index("• Transport — 650")
    assert "Largest: Lunch at Sultan's Dine — 850" in text
    assert "💰 Income: 45,000 BDT" in text
    assert "• Salary — 45,000" in text
    assert "🤝 Loans & debts" in text
    assert "• Borrowed 5,000 from Rahim" in text
    assert "• Lent 2,000 to Karim" in text
    assert "• Paid back 1,000 to Rahim" in text
    assert "• Received 500 from Karim" in text
    assert "Open debts: you owe 9,000 · owed to you 3,500 (BDT)" in text


def test_format_yesterday_overview_top_five_categories_only():
    categories = [(f"Cat{i}", 100 - i) for i in range(7)]

    text = format_yesterday_overview(
        date_label="Sat 14 Nov 2026",
        expense_total=679,
        expense_by_category=categories,
        income_total=0,
        income_by_category=[],
    )

    assert "• Cat4" in text
    assert "• Cat5" not in text
    assert "💰 Income" not in text
    assert "🤝 Loans & debts" not in text
    assert "Open debts" not in text


def test_format_yesterday_overview_no_activity():
    text = format_yesterday_overview(
        date_label="Sat 14 Nov 2026",
        expense_total=0,
        expense_by_category=[],
        income_total=0,
        income_by_category=[],
    )

    assert "📊 Yesterday — Sat 14 Nov 2026" in text
    assert "No money activity recorded yesterday." in text
    assert "💸 Expenses" not in text
    assert "Open debts" not in text


def test_format_yesterday_overview_no_activity_but_open_debts():
    text = format_yesterday_overview(
        date_label="Sat 14 Nov 2026",
        expense_total=0,
        expense_by_category=[],
        income_total=0,
        income_by_category=[],
        owed_by_user=4000,
        owed_to_user=0,
    )

    assert "No money activity recorded yesterday." in text
    assert "Open debts: you owe 4,000 · owed to you 0 (BDT)" in text


# --- tool declarations & dispatch ------------------------------------------


def test_finance_tools_are_declared():
    names = {tool["name"] for tool in ai_tools.TOOL_DECLARATIONS}

    assert {
        "add_transaction",
        "get_finance_summary",
        "list_transactions",
        "add_debt",
        "record_debt_payment",
        "list_debts",
        "set_expense_reminders",
        "get_daily_overview",
        "set_daily_overview",
    } <= names


def test_finance_mutating_tools_are_deduped():
    assert {
        "add_transaction",
        "add_debt",
        "record_debt_payment",
        "set_expense_reminders",
        "set_daily_overview",
    } <= MUTATING_TOOLS


class _FakeSession:
    """Minimal async session: scalar() answers the timezone lookup."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def scalar(self, *args, **kwargs):
        return "Asia/Dhaka"

    def add(self, obj):
        pass

    async def commit(self):
        pass

    async def flush(self):
        pass


def _session_factory():
    return _FakeSession()


def _patch_actor(monkeypatch, user_id):
    async def fake_resolve(session, telegram_user_id):
        assert telegram_user_id == 42
        return SimpleNamespace(id=user_id)

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )


async def test_add_transaction_dispatch(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    captured = {}

    async def fake_add(session, uid, **kwargs):
        captured.update(user_id=uid, **kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            type=kwargs["type"],
            amount=kwargs["amount"],
            currency="BDT",
            category="Food",
            note=kwargs.get("note"),
            occurred_at=datetime(2026, 11, 15, 7, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(finance_service, "add_transaction", fake_add)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="add_transaction",
        arguments={
            "type": "expense",
            "amount": 250,
            "category": "food",
            "note": "lunch",
        },
    )

    assert result["success"] is True
    assert captured["user_id"] == user_id
    assert captured["type"] == "expense"
    assert captured["amount"] == Decimal("250.00")
    assert captured["category"] == "food"  # normalized in the service
    assert result["transaction"]["amount"] == 250.0
    assert result["transaction"]["currency"] == "BDT"
    assert "Asia/Dhaka" in result["transaction"]["occurred_at_local"]


async def test_add_transaction_dispatch_rejects_bad_amount(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="add_transaction",
        arguments={"type": "expense", "amount": -5, "category": "Food"},
    )

    assert result["success"] is False
    assert "positive" in result["error"]


async def test_record_debt_payment_dispatch_resolves_short_id(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    debt_id = uuid.UUID("ab12cd34-0000-4000-8000-000000000000")
    debt = SimpleNamespace(
        id=debt_id,
        direction="borrowed",
        kind="debt",
        counterparty="Rahim",
        principal=Decimal("5000"),
        remaining=Decimal("4000"),
        note=None,
        due_date=None,
        status="open",
    )

    async def fake_list(session, uid, **kwargs):
        return [debt]

    captured = {}

    async def fake_pay(session, uid, did, amount):
        captured.update(user_id=uid, debt_id=did, amount=amount)
        return debt

    monkeypatch.setattr(finance_service, "list_debts", fake_list)
    monkeypatch.setattr(finance_service, "record_debt_payment", fake_pay)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="record_debt_payment",
        arguments={"debt_id": "ab12cd34", "amount": 1000},
    )

    assert result["success"] is True
    assert captured == {
        "user_id": user_id,
        "debt_id": debt_id,
        "amount": Decimal("1000.00"),
    }
    assert result["debt"]["short_id"] == "ab12cd34"
    assert result["debt"]["remaining"] == 4000.0


async def test_get_daily_overview_dispatch(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    async def fake_build(session, uid, tz, now, *, day="yesterday"):
        assert day == "yesterday"
        return "📊 Yesterday — overview text"

    monkeypatch.setattr(finance_service, "build_day_overview", fake_build)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="get_daily_overview",
        arguments={},
    )

    assert result["success"] is True
    assert result["day"] == "yesterday"
    assert result["overview"] == "📊 Yesterday — overview text"


async def test_set_expense_reminders_dispatch(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    captured = {}

    async def fake_set(session, user, **kwargs):
        captured.update(kwargs)
        return {
            "enabled": True,
            "times_per_day": kwargs["times_per_day"],
            "times": ["10:00", "14:00", "21:00"],
        }

    monkeypatch.setattr(finance_service, "set_expense_nudges", fake_set)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="set_expense_reminders",
        arguments={"enabled": True, "times_per_day": 3},
    )

    assert result["success"] is True
    assert captured["enabled"] is True
    assert captured["times_per_day"] == 3
    assert captured["timezone_name"] == "Asia/Dhaka"
    assert result["times"] == ["10:00", "14:00", "21:00"]


def test_system_instruction_includes_finance_rules():
    instruction = build_system_instruction("user")

    assert "FINANCE RULES" in instruction
    assert "Expense tracking is implemented" in instruction
    assert "BDT (taka)" in instruction
    assert "Never invent" in instruction


# --- delivery worker: special job types -------------------------------------


class _DeliverySession:
    """Fake session backed by a shared store dict (delivery tests)."""

    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, *args, **kwargs):
        jobs = self.store.get("due_jobs", [])
        return SimpleNamespace(scalars=lambda: list(jobs))

    async def get(self, model, pk):
        return self.store["entities"].get((model.__name__, pk))

    async def scalar(self, *args, **kwargs):
        return self.store.get("telegram_user_id")

    def add(self, obj):
        self.store.setdefault("added", []).append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.store["commits"] = self.store.get("commits", 0) + 1


class _FakeBot:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    async def send_message(self, chat_id, text):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=777)


def _delivery_store(*, job_type="reminder", recurrence_rule=None):
    job_id = uuid.uuid4()
    occurrence_id = uuid.uuid4()
    reminder_id = uuid.uuid4()
    user_id = uuid.uuid4()

    job = SimpleNamespace(
        id=job_id,
        occurrence_id=occurrence_id,
        job_type=job_type,
        status="pending",
        attempts=0,
        max_attempts=5,
        run_at=datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc),
        locked_until=None,
        locked_by=None,
        last_error=None,
    )
    occurrence = SimpleNamespace(
        id=occurrence_id,
        reminder_id=reminder_id,
        scheduled_for=datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc),
        status="scheduled",
        completed_at=None,
    )
    reminder = SimpleNamespace(
        id=reminder_id,
        user_id=user_id,
        title="🧾 Expense check-in",
        description="Log what you spent or earned since last time.",
        status="active",
        recurrence_rule=recurrence_rule,
        timezone="Asia/Dhaka",
    )

    store = {
        "due_jobs": [job],
        "entities": {
            (DeliveryJob.__name__, job_id): job,
            (ReminderOccurrence.__name__, occurrence_id): occurrence,
            (Reminder.__name__, reminder_id): reminder,
        },
        "telegram_user_id": 4242,
        "added": [],
    }
    return store, job, occurrence, reminder


def _factory_for(store):
    def factory():
        return _DeliverySession(store)

    return factory


NOW_UTC = datetime(2026, 11, 15, 4, 5, tzinfo=timezone.utc)


async def test_delivery_daily_overview_sends_overview_text(monkeypatch):
    store, job, occurrence, reminder = _delivery_store(
        job_type="daily_overview",
        recurrence_rule="FREQ=DAILY",
    )
    reminder.title = "🧾 Yesterday's money overview"
    reminder.description = None
    bot = _FakeBot()

    async def fake_overview(session, user_id, tz, now):
        return "📊 Yesterday — Sat 14 Nov 2026\n💸 Expenses: 1,850 BDT"

    monkeypatch.setattr(
        finance_service,
        "build_yesterday_overview",
        fake_overview,
    )

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary["succeeded"] == 1
    assert bot.sent == [
        (4242, "📊 Yesterday — Sat 14 Nov 2026\n💸 Expenses: 1,850 BDT")
    ]
    assert "⏰ Reminder:" not in bot.sent[0][1]
    assert job.status == "succeeded"

    # The follow-up job keeps the daily_overview type.
    new_jobs = [
        obj for obj in store["added"] if isinstance(obj, DeliveryJob)
    ]
    assert len(new_jobs) == 1
    assert new_jobs[0].job_type == "daily_overview"


async def test_delivery_daily_overview_falls_back_to_static_text(monkeypatch):
    store, job, occurrence, reminder = _delivery_store(
        job_type="daily_overview",
    )
    reminder.title = "🧾 Yesterday's money overview"
    reminder.description = None
    bot = _FakeBot()

    async def broken_overview(session, user_id, tz, now):
        raise RuntimeError("db gone")

    monkeypatch.setattr(
        finance_service,
        "build_yesterday_overview",
        broken_overview,
    )

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary["succeeded"] == 1
    assert bot.sent == [(4242, "⏰ Reminder: 🧾 Yesterday's money overview")]


async def test_delivery_expense_nudge_skipped_after_recent_activity(monkeypatch):
    store, job, occurrence, reminder = _delivery_store(
        job_type="expense_nudge",
        recurrence_rule="FREQ=DAILY",
    )
    bot = _FakeBot()

    async def fake_last_time(session, user_id):
        return NOW_UTC - timedelta(hours=1)

    monkeypatch.setattr(
        finance_service,
        "last_transaction_time",
        fake_last_time,
    )

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert bot.sent == []  # nothing sent
    assert summary["succeeded"] == 1
    assert job.status == "succeeded"
    assert occurrence.status == "delivered"

    attempts = [
        obj
        for obj in store["added"]
        if obj.__class__.__name__ == "DeliveryAttempt"
    ]
    assert len(attempts) == 1
    assert attempts[0].result == "skipped"
    assert attempts[0].error_message == "recent activity"

    # The next occurrence is still scheduled, as an expense nudge.
    new_jobs = [
        obj for obj in store["added"] if isinstance(obj, DeliveryJob)
    ]
    assert len(new_jobs) == 1
    assert new_jobs[0].job_type == "expense_nudge"


async def test_delivery_expense_nudge_sent_when_no_recent_activity(monkeypatch):
    store, job, occurrence, reminder = _delivery_store(
        job_type="expense_nudge",
    )
    bot = _FakeBot()

    async def fake_last_time(session, user_id):
        return NOW_UTC - timedelta(hours=5)

    monkeypatch.setattr(
        finance_service,
        "last_transaction_time",
        fake_last_time,
    )

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary["succeeded"] == 1
    assert len(bot.sent) == 1
    assert bot.sent[0][1].startswith("⏰ Reminder: 🧾 Expense check-in")
