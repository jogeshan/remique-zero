"""Tests for Phases 6+7 reminders and the delivery worker.

Covers the pure recurrence/parsing helpers, the AI tool dispatch
for the reminder tools, the delivery worker's claim/deliver/retry
decisions with fake sessions and a fake bot, and the system-prompt
reminder section. Real-DB behaviour is left to the integration
suite (aiosqlite is not available in the test environment).
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.models import DeliveryJob, Reminder, ReminderOccurrence
from app.services import ai_tools, delivery_service, memory_service
from app.services import reminder_service
from app.services.ai_service import build_system_instruction
from app.services.reminder_service import (
    compute_next_occurrence,
    parse_recurrence,
    parse_reminder_text,
)

DHAKA = ZoneInfo("Asia/Dhaka")
# Fixed "now" for parser tests: a Sunday at 10:00 in Dhaka.
NOW_LOCAL = datetime(2026, 11, 15, 10, 0, tzinfo=DHAKA)


# --- parse_recurrence -------------------------------------------------


def test_parse_recurrence_none_and_enum():
    assert parse_recurrence(None) is None
    assert parse_recurrence("") is None
    assert parse_recurrence("daily") == "FREQ=DAILY"
    assert parse_recurrence("Weekly") == "FREQ=WEEKLY"
    assert parse_recurrence(" weekdays ") == (
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    )


def test_parse_recurrence_rejects_unknown_values():
    with pytest.raises(ValueError):
        parse_recurrence("monthly")

    with pytest.raises(ValueError):
        parse_recurrence(123)


# --- compute_next_occurrence -------------------------------------------


def test_compute_next_occurrence_no_recurrence():
    anchor = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)

    assert compute_next_occurrence(anchor, None, anchor) is None
    assert compute_next_occurrence(anchor, "NOT A RULE", anchor) is None


def test_compute_next_occurrence_daily_is_strictly_after():
    anchor = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
    rule = parse_recurrence("daily")

    # Exactly at an occurrence: the NEXT one is a day later.
    assert compute_next_occurrence(anchor, rule, anchor) == (
        anchor + timedelta(days=1)
    )
    # Before the anchor: the anchor itself is next.
    assert compute_next_occurrence(
        anchor, rule, anchor - timedelta(hours=1)
    ) == anchor
    # Between occurrences: the upcoming one.
    assert compute_next_occurrence(
        anchor, rule, anchor + timedelta(hours=12)
    ) == anchor + timedelta(days=1)


def test_compute_next_occurrence_weekly():
    anchor = datetime(2026, 11, 15, 18, 30, tzinfo=timezone.utc)  # Sun
    rule = parse_recurrence("weekly")

    assert compute_next_occurrence(anchor, rule, anchor) == (
        anchor + timedelta(days=7)
    )


def test_compute_next_occurrence_weekdays_skips_weekend():
    friday = datetime(2026, 11, 20, 7, 0, tzinfo=timezone.utc)
    rule = parse_recurrence("weekdays")

    # Friday -> Monday.
    assert compute_next_occurrence(friday, rule, friday) == (
        friday + timedelta(days=3)
    )

    monday = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)
    assert compute_next_occurrence(monday, rule, monday) == (
        monday + timedelta(days=1)
    )


# --- parse_reminder_text -------------------------------------------------


def test_parse_reminder_text_tomorrow_at_9am():
    result = parse_reminder_text(
        "submit the report tomorrow at 9am",
        DHAKA,
        NOW_LOCAL,
    )

    assert result is not None
    title, first_run, recurrence = result
    assert title == "submit the report"
    assert first_run == datetime(2026, 11, 16, 9, 0, tzinfo=DHAKA)
    assert recurrence is None


def test_parse_reminder_text_in_2_hours():
    result = parse_reminder_text(
        "call mom in 2 hours",
        DHAKA,
        NOW_LOCAL,
    )

    assert result is not None
    title, first_run, recurrence = result
    assert title == "call mom"
    assert first_run == datetime(2026, 11, 15, 12, 0, tzinfo=DHAKA)
    assert recurrence is None


def test_parse_reminder_text_every_sunday():
    result = parse_reminder_text(
        "prepare the weekly report every Sunday at 18:30",
        DHAKA,
        NOW_LOCAL,
    )

    assert result is not None
    title, first_run, recurrence = result
    assert title == "prepare the weekly report"
    # Today is Sunday and 18:30 is still ahead.
    assert first_run == datetime(2026, 11, 15, 18, 30, tzinfo=DHAKA)
    assert recurrence == "weekly"


def test_parse_reminder_text_every_weekday_rolls_to_monday():
    result = parse_reminder_text(
        "gym every weekday at 7am",
        DHAKA,
        NOW_LOCAL,
    )

    assert result is not None
    title, first_run, recurrence = result
    assert title == "gym"
    # Today is Sunday: the first weekday slot is Monday 07:00.
    assert first_run == datetime(2026, 11, 16, 7, 0, tzinfo=DHAKA)
    assert recurrence == "weekdays"


def test_parse_reminder_text_without_date_returns_none():
    assert parse_reminder_text("buy milk", DHAKA, NOW_LOCAL) is None
    assert parse_reminder_text("", DHAKA, NOW_LOCAL) is None


# --- tool declarations ---------------------------------------------------


def test_reminder_tools_are_declared():
    names = {tool["name"] for tool in ai_tools.TOOL_DECLARATIONS}

    assert {
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
        "snooze_reminder",
    } <= names


# --- execute_ai_tool dispatch ---------------------------------------------


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


async def test_create_reminder_rejects_past_time(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    calls = []

    async def fake_create(session, user, **kwargs):
        calls.append(kwargs)
        raise AssertionError("create_reminder must not be called")

    monkeypatch.setattr(
        reminder_service,
        "create_reminder",
        fake_create,
    )

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="create_reminder",
        arguments={
            "title": "Old task",
            "first_run_at": "2020-01-01T09:00:00+06:00",
        },
    )

    assert result["success"] is False
    assert "past" in result["error"]
    assert calls == []


async def test_create_reminder_dispatches_with_utc_time(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    captured = {}

    async def fake_create(session, user, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=kwargs["title"],
            recurrence_rule="FREQ=DAILY",
        )

    monkeypatch.setattr(
        reminder_service,
        "create_reminder",
        fake_create,
    )

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="create_reminder",
        arguments={
            "title": "Take medicine",
            "first_run_at": "2999-11-16T09:00:00+06:00",
            "recurrence": "daily",
        },
    )

    assert result["success"] is True
    # 09:00 at +06:00 is 03:00 UTC, and the enum passes through.
    assert captured["first_run_at"] == datetime(
        2999, 11, 16, 3, 0, tzinfo=timezone.utc
    )
    assert captured["recurrence"] == "daily"
    assert captured["timezone_name"] == "Asia/Dhaka"
    assert result["reminder"]["recurrence"] == "daily"
    assert "Asia/Dhaka" in result["reminder"]["first_run_local"]


async def test_create_reminder_rejects_bad_recurrence(monkeypatch):
    user_id = uuid.uuid4()
    _patch_actor(monkeypatch, user_id)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="create_reminder",
        arguments={
            "title": "Something",
            "first_run_at": "2999-11-16T09:00:00+06:00",
            "recurrence": "monthly",
        },
    )

    assert result["success"] is False
    assert "recurrence" in result["error"]


# --- delivery helpers ------------------------------------------------------


def test_retry_delay_and_give_up():
    assert delivery_service.retry_delay(1) == timedelta(minutes=5)
    assert delivery_service.retry_delay(3) == timedelta(minutes=15)
    assert delivery_service.should_give_up(5, 5) is True
    assert delivery_service.should_give_up(4, 5) is False


# --- delivery worker ---------------------------------------------------------


class _DeliverySession:
    """Fake session backed by a shared store dict."""

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


def _delivery_store(*, reminder_status="active", recurrence_rule=None):
    job_id = uuid.uuid4()
    occurrence_id = uuid.uuid4()
    reminder_id = uuid.uuid4()
    user_id = uuid.uuid4()

    job = SimpleNamespace(
        id=job_id,
        occurrence_id=occurrence_id,
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
        title="Pay rent",
        description=None,
        status=reminder_status,
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


async def test_delivery_success_completes_one_time_reminder():
    store, job, occurrence, reminder = _delivery_store()
    bot = _FakeBot()

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary == {
        "claimed": 1,
        "succeeded": 1,
        "failed": 0,
        "cancelled": 0,
    }
    assert bot.sent == [(4242, "⏰ Reminder: Pay rent")]
    assert job.status == "succeeded"
    assert job.attempts == 1  # counted at claim time
    assert occurrence.status == "delivered"
    assert reminder.status == "completed"

    attempts = [
        obj for obj in store["added"] if obj.__class__.__name__ == "DeliveryAttempt"
    ]
    assert len(attempts) == 1
    assert attempts[0].result == "succeeded"
    assert attempts[0].provider_message_id == "777"


async def test_delivery_failure_retries_with_backoff():
    store, job, occurrence, reminder = _delivery_store()
    bot = _FakeBot(error=RuntimeError("telegram down"))

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary["failed"] == 1
    assert job.status == "pending"
    assert job.run_at == NOW_UTC + timedelta(minutes=5)
    assert job.locked_until is None
    assert "telegram down" in job.last_error
    assert occurrence.status == "scheduled"


async def test_delivery_failure_gives_up_at_max_attempts():
    store, job, occurrence, reminder = _delivery_store()
    job.attempts = 4  # claim increments to 5 == max_attempts
    bot = _FakeBot(error=RuntimeError("telegram down"))

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary["failed"] == 1
    assert job.status == "failed"


async def test_delivery_skips_cancelled_reminder():
    store, job, occurrence, reminder = _delivery_store(
        reminder_status="cancelled"
    )
    bot = _FakeBot()

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary["cancelled"] == 1
    assert bot.sent == []
    assert job.status == "cancelled"
    assert occurrence.status == "skipped"


async def test_delivery_recurring_schedules_next_occurrence():
    store, job, occurrence, reminder = _delivery_store(
        recurrence_rule="FREQ=DAILY"
    )
    bot = _FakeBot()

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary["succeeded"] == 1
    assert reminder.status == "active"  # recurring stays active

    new_occurrences = [
        obj for obj in store["added"] if isinstance(obj, ReminderOccurrence)
    ]
    new_jobs = [
        obj for obj in store["added"] if isinstance(obj, DeliveryJob)
    ]

    assert len(new_occurrences) == 1
    assert len(new_jobs) == 1
    # 09:00 Dhaka daily: 2026-11-15 03:00 UTC -> next day 03:00 UTC.
    assert new_occurrences[0].scheduled_for == datetime(
        2026, 11, 16, 3, 0, tzinfo=timezone.utc
    )
    assert new_jobs[0].idempotency_key == (
        f"reminder:{new_occurrences[0].id}"
    )
    assert new_jobs[0].status == "pending"


async def test_delivery_nothing_due():
    store, job, occurrence, reminder = _delivery_store()
    store["due_jobs"] = []
    bot = _FakeBot()

    summary = await delivery_service.process_due_deliveries(
        _factory_for(store),
        bot,
        now=NOW_UTC,
    )

    assert summary == {
        "claimed": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
    }
    assert bot.sent == []


# --- system prompt -----------------------------------------------------------


def test_system_instruction_includes_current_time_and_reminder_rules():
    instruction = build_system_instruction(
        role="user",
        display_name="Test",
        timezone_name="Asia/Dhaka",
        now_local=NOW_LOCAL,
    )

    assert "Current date and time for the user" in instruction
    assert "Sunday 15 November 2026, 10:00" in instruction
    assert "create_reminder" in instruction
    assert "not implemented" not in instruction.lower().split(
        "reminder rules:"
    )[1].split("conversation rules:")[0]
