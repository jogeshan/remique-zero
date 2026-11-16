"""Tests for the delivery next-due cache (delivery_service)."""

from datetime import datetime, timedelta, timezone

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


def test_should_run_when_nothing_known_pending():
    assert delivery_service.should_run_delivery_pass(NOW, None, NOW)


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


def test_should_run_when_stale_even_if_nothing_due_yet():
    assert delivery_service.should_run_delivery_pass(
        NOW,
        NOW + timedelta(hours=1),
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
