"""Tests for the delivery next-due cache (delivery_service)."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import delivery_service

NOW = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_hint():
    delivery_service.clear_next_due_hint()
    yield
    delivery_service.clear_next_due_hint()


def test_note_next_due_keeps_earliest():
    later = NOW + timedelta(hours=2)
    earlier = NOW + timedelta(hours=1)

    delivery_service.note_next_due(later)
    assert delivery_service.get_next_due_hint() == later

    delivery_service.note_next_due(earlier)
    assert delivery_service.get_next_due_hint() == earlier

    delivery_service.note_next_due(later)
    assert delivery_service.get_next_due_hint() == earlier


def test_note_next_due_ignores_none():
    delivery_service.note_next_due(None)
    assert delivery_service.get_next_due_hint() is None


def test_should_skip_when_nothing_pending_and_pass_is_fresh():
    assert not delivery_service.should_run_delivery_pass(NOW, None, NOW)


def test_should_run_when_nothing_pending_and_quiet_staleness_reached():
    assert delivery_service.should_run_delivery_pass(
        NOW,
        None,
        NOW - timedelta(seconds=1201),
    )


def test_should_run_when_earliest_is_due():
    assert delivery_service.should_run_delivery_pass(
        NOW,
        NOW - timedelta(seconds=1),
        NOW,
    )


def test_should_skip_when_next_due_is_far_and_pass_is_fresh():
    assert not delivery_service.should_run_delivery_pass(
        NOW,
        NOW + timedelta(hours=1),
        NOW - timedelta(seconds=10),
    )


def test_quiet_staleness_is_not_sixty_seconds():
    # A job an hour away is outside the active window, so the
    # quiet 20-minute staleness applies: a 61-second-old pass
    # must NOT trigger another database hit (that is what lets
    # Neon suspend between passes).
    assert not delivery_service.should_run_delivery_pass(
        NOW,
        NOW + timedelta(hours=1),
        NOW - timedelta(seconds=61),
    )
    assert delivery_service.should_run_delivery_pass(
        NOW,
        NOW + timedelta(hours=1),
        NOW - timedelta(seconds=1201),
    )


def test_active_window_uses_tight_staleness():
    # A job 10 minutes out is inside the active window: the
    # tight 60-second staleness applies.
    assert not delivery_service.should_run_delivery_pass(
        NOW,
        NOW + timedelta(minutes=10),
        NOW - timedelta(seconds=30),
    )
    assert delivery_service.should_run_delivery_pass(
        NOW,
        NOW + timedelta(minutes=10),
        NOW - timedelta(seconds=61),
    )


def test_should_run_on_first_pass():
    assert delivery_service.should_run_delivery_pass(
        NOW,
        NOW + timedelta(hours=1),
        None,
    )


class _ScalarSession:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def scalar(self, statement):
        return self.value


@pytest.mark.asyncio
async def test_get_next_due_at_returns_scalar():
    value = NOW + timedelta(minutes=30)
    factory = lambda: _ScalarSession(value)  # noqa: E731
    assert await delivery_service.get_next_due_at(factory) == value

    factory_none = lambda: _ScalarSession(None)  # noqa: E731
    assert await delivery_service.get_next_due_at(factory_none) is None


# --- memory keyword search -------------------------------------------


class _ScalarsSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return list(self.rows)


@pytest.mark.asyncio
async def test_search_memories_uses_scalars_and_returns_rows():
    from app.services import memory_service

    rows = [SimpleNamespace(content="My cat is named Mochi")]
    session = _ScalarsSession(rows)

    found = await memory_service.search_memories(
        session,
        uuid.uuid4(),
        "cat",
    )

    assert found == rows
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_search_memories_empty_query_falls_back_to_list(monkeypatch):
    from app.services import memory_service

    async def fake_list(session, user_id, limit=50):
        return ["latest"]

    monkeypatch.setattr(memory_service, "list_memories", fake_list)

    found = await memory_service.search_memories(
        _ScalarsSession([]),
        uuid.uuid4(),
        "   ",
    )

    assert found == ["latest"]


def test_recall_memories_tool_accepts_query():
    from app.services.ai_tools import TOOL_DECLARATIONS

    recall = next(
        tool for tool in TOOL_DECLARATIONS if tool["name"] == "recall_memories"
    )
    properties = recall["parameters_json_schema"]["properties"]
    assert "query" in properties
    assert "limit" in properties
