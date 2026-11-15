"""Tests for Phase 12: production hardening.

Covers the security headers middleware, the fixed-window rate
limiter (unit-tested directly with a low limit and a fake clock,
rather than hammering the HTTP layer), the /health and
/health/deep endpoints (fake sessions for the ok / degraded /
stale-migration paths), the API error shape, and the data-export
builder (structure, completeness, and per-user scoping) using
the same in-memory fake-session style as the rest of the suite.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    UnaryExpression,
)

from app.api.health import _alembic_head
from app.bot import USER_BOT_COMMANDS
from app.database import get_db_session
from app.main import app
from app.middleware import FixedWindowRateLimiter
from app.models import (
    Debt,
    DebtPayment,
    Reminder,
    ReminderOccurrence,
    Task,
    TelegramAccount,
    Transaction,
    User,
    UserMemory,
    UserSetting,
)
from app.services import export_service

NOW = datetime.now(timezone.utc)


# --- fake session (same style as tests/test_dashboard.py) -----------------


def _clause_value(right):
    if isinstance(right, BindParameter):
        return right.value
    return None


def _matches(row, clause) -> bool:
    if clause is None:
        return True

    if isinstance(clause, BooleanClauseList):
        return all(_matches(row, part) for part in clause.clauses)

    if isinstance(clause, UnaryExpression):
        return not _matches(row, clause.element)

    if isinstance(clause, BinaryExpression):
        key = getattr(clause.left, "key", None) or getattr(
            clause.left, "name", None
        )
        value = getattr(row, key, None)
        expected = _clause_value(clause.right)
        op = clause.operator

        if op is operators.eq:
            return value == expected
        if op is operators.ne:
            return value != expected
        if op is operators.is_:
            return (value is None) if expected is None else value == expected
        if op is operators.isnot:
            return (value is not None) if expected is None else value != expected
        if op is operators.ge:
            return value is not None and value >= expected
        if op is operators.gt:
            return value is not None and value > expected
        if op is operators.le:
            return value is not None and value <= expected
        if op is operators.lt:
            return value is not None and value < expected

        raise AssertionError(f"Unsupported operator: {op}")

    raise AssertionError(f"Unsupported clause: {clause!r}")


class FakeSession:
    """In-memory AsyncSession stand-in (entity/column selects)."""

    def __init__(self):
        self.tables: dict[str, list] = {}
        self.committed = False

    def seed(self, *objects):
        for obj in objects:
            self.tables.setdefault(obj.__tablename__, []).append(obj)
        return self

    def add(self, obj):
        self.tables.setdefault(obj.__tablename__, []).append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def flush(self):
        pass

    async def scalar(self, query):
        rows = self._evaluate(query)
        return rows[0] if rows else None

    async def scalars(self, query):
        return self._evaluate(query)

    def _filtered(self, table_name, query):
        rows = list(self.tables.get(table_name, []))
        for criterion in query._where_criteria:
            rows = [row for row in rows if _matches(row, criterion)]
        return rows

    def _ordered(self, rows, query):
        clauses = list(query._order_by_clauses or [])
        for clause in reversed(clauses):
            reverse = False
            element = clause
            if isinstance(clause, UnaryExpression):
                if clause.modifier is operators.desc_op:
                    reverse = True
                    element = clause.element
                elif clause.modifier is operators.asc_op:
                    element = clause.element
            key = getattr(element, "key", None) or getattr(
                element, "name", None
            )
            rows.sort(
                key=lambda row, k=key: (
                    getattr(row, k) is None,
                    getattr(row, k),
                ),
                reverse=reverse,
            )
        return rows

    def _evaluate(self, query):
        description = query.column_descriptions[0]
        entity = description.get("entity")

        if entity is not None and description["expr"] is entity:
            rows = self._filtered(entity.__tablename__, query)
            rows = self._ordered(rows, query)
            limit = query._limit_clause
            if limit is not None:
                rows = rows[: limit.value]
            return rows

        expr = description["expr"]

        if hasattr(expr, "table"):
            rows = self._filtered(expr.table.name, query)
            rows = self._ordered(rows, query)
            limit = query._limit_clause
            if limit is not None:
                rows = rows[: limit.value]
            return [getattr(row, expr.key) for row in rows]

        table_name = query.get_final_froms()[0].name
        return [len(self._filtered(table_name, query))]


# --- security headers -------------------------------------------------------


@pytest.fixture()
def plain_client():
    """TestClient with the DB session dependency stubbed out.

    Enough for requests that fail authentication before any
    query runs, and for the dashboard page (no DB access).
    """

    async def override_session():
        yield object()

    app.dependency_overrides[get_db_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def test_security_headers_on_dashboard_and_api(plain_client):
    client = plain_client

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    csp = page.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "img-src 'self' data:" in csp

    # API responses (including errors) carry the headers too.
    api = client.get("/api/v1/me")
    assert api.status_code == 401
    assert api.headers["x-content-type-options"] == "nosniff"
    assert api.headers["x-frame-options"] == "DENY"


# --- API error shape ----------------------------------------------------------


def test_api_errors_use_error_key_and_other_paths_keep_detail(
    plain_client,
):
    client = plain_client

    api = client.get("/api/v1/me")
    assert api.status_code == 401
    assert api.json() == {
        "error": "Missing or invalid API token."
    }
    # The auth challenge header survives the custom handler.
    assert api.headers["www-authenticate"] == "Bearer"

    missing = client.get("/no-such-path")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}


# --- rate limiter (unit) ------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def test_rate_limiter_blocks_after_limit_and_resets_next_window():
    clock = FakeClock()
    limiter = FixedWindowRateLimiter(
        limit=3, window_seconds=60.0, clock=clock
    )

    assert limiter.allow("10.0.0.1") is True
    assert limiter.allow("10.0.0.1") is True
    assert limiter.allow("10.0.0.1") is True
    assert limiter.allow("10.0.0.1") is False
    assert limiter.allow("10.0.0.1") is False

    # A different client has its own bucket.
    assert limiter.allow("10.0.0.2") is True

    # The next window resets the count.
    clock.now += 61.0
    assert limiter.allow("10.0.0.1") is True


def test_rate_limiter_rejects_invalid_limit():
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(limit=0)


# --- health endpoints ---------------------------------------------------------


class FakeHealthSession:
    """Minimal session for /health/deep: execute + scalar."""

    def __init__(self, revision=None, fail=False):
        self._revision = revision
        self._fail = fail

    async def execute(self, clause):
        if self._fail:
            raise RuntimeError("connection refused")
        return None

    async def scalar(self, clause):
        if self._fail:
            raise RuntimeError("connection refused")
        return self._revision


@pytest.fixture()
def health_client():
    holder = {}

    async def override_session():
        yield holder["session"]

    app.dependency_overrides[get_db_session] = override_session
    try:
        yield TestClient(app), holder
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def test_health_shallow_is_ok_without_database(health_client):
    client, holder = health_client
    holder["session"] = FakeHealthSession(fail=True)

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_deep_ok_when_database_at_head(health_client):
    client, holder = health_client
    holder["session"] = FakeHealthSession(revision=_alembic_head())

    response = client.get("/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["alembic"]["current"] == _alembic_head()
    assert body["alembic"]["head"] == _alembic_head()
    assert body["alembic"]["up_to_date"] is True
    assert body["detail"] is None


def test_health_deep_degraded_when_database_fails(health_client):
    client, holder = health_client
    holder["session"] = FakeHealthSession(fail=True)

    response = client.get("/health/deep")
    # Degraded is reported in the payload, never as a 500.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"
    assert body["detail"]


def test_health_deep_degraded_when_migrations_behind(health_client):
    client, holder = health_client
    holder["session"] = FakeHealthSession(
        revision="d22aaa61a3f2"  # the initial schema revision
    )

    body = client.get("/health/deep").json()
    assert body["status"] == "degraded"
    assert body["database"] == "ok"
    assert body["alembic"]["up_to_date"] is False
    assert "migrations" in body["detail"]


# --- data export --------------------------------------------------------------


def _user(name="Alice"):
    return User(
        id=uuid.uuid4(),
        display_name=name,
        status="active",
        role="user",
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )


def build_export_session():
    alice = _user("Alice")
    bob = _user("Bob")
    earlier = NOW - timedelta(days=2)

    session = FakeSession()
    session.seed(
        alice,
        bob,
        TelegramAccount(
            user_id=alice.id,
            telegram_user_id=111111,
            chat_id=111111,
            username="alice_tg",
            linked_at=earlier,
        ),
        TelegramAccount(
            user_id=bob.id,
            telegram_user_id=222222,
            chat_id=222222,
            username="bob_tg",
            linked_at=earlier,
        ),
        UserSetting(
            user_id=alice.id,
            timezone="Asia/Dhaka",
            language="en",
        ),
        UserMemory(
            id=uuid.uuid4(),
            user_id=alice.id,
            content="My cat is named Miso",
            category="fact",
            created_at=NOW,
            updated_at=NOW,
        ),
        UserMemory(
            id=uuid.uuid4(),
            user_id=bob.id,
            content="Bob's private memory",
            category="fact",
            created_at=NOW,
            updated_at=NOW,
        ),
        Transaction(
            id=uuid.uuid4(),
            user_id=alice.id,
            type="expense",
            amount=Decimal("250"),
            currency="BDT",
            category="Food",
            note="lunch",
            occurred_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        ),
        Transaction(
            id=uuid.uuid4(),
            user_id=bob.id,
            type="expense",
            amount=Decimal("9999"),
            currency="BDT",
            category="Food",
            note="bob feast",
            occurred_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        ),
        Task(
            id=uuid.uuid4(),
            user_id=alice.id,
            title="Buy milk",
            status="open",
            created_at=NOW,
            updated_at=NOW,
        ),
        Task(
            id=uuid.uuid4(),
            user_id=bob.id,
            title="Bob's task",
            status="open",
            created_at=NOW,
            updated_at=NOW,
        ),
    )

    reminder = Reminder(
        id=uuid.uuid4(),
        user_id=alice.id,
        title="Drink water",
        timezone="Asia/Dhaka",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    session.seed(
        reminder,
        ReminderOccurrence(
            id=uuid.uuid4(),
            reminder_id=reminder.id,
            scheduled_for=NOW + timedelta(hours=2),
            status="scheduled",
            created_at=NOW,
        ),
    )

    debt = Debt(
        id=uuid.uuid4(),
        user_id=alice.id,
        direction="borrowed",
        kind="debt",
        counterparty="Karim",
        principal=Decimal("1000"),
        remaining=Decimal("400"),
        status="open",
        created_at=earlier,
        updated_at=NOW,
    )
    session.seed(
        debt,
        DebtPayment(
            id=uuid.uuid4(),
            debt_id=debt.id,
            user_id=alice.id,
            amount=Decimal("600"),
            paid_at=earlier + timedelta(days=1),
            created_at=earlier + timedelta(days=1),
        ),
        Debt(
            id=uuid.uuid4(),
            user_id=bob.id,
            direction="lent",
            kind="debt",
            counterparty="BobCounterparty",
            principal=Decimal("777"),
            remaining=Decimal("777"),
            status="open",
            created_at=NOW,
            updated_at=NOW,
        ),
    )

    return session, alice, bob


async def test_export_builder_returns_complete_scoped_document():
    session, alice, _bob = build_export_session()

    document = await export_service.build_user_export(session, alice)

    # Metadata: format, caps and counts.
    meta = document["export"]
    assert meta["format_version"] == export_service.EXPORT_FORMAT_VERSION
    assert meta["caps"]["transactions"] == export_service.MAX_TRANSACTIONS
    assert meta["counts"] == {
        "memories": 1,
        "reminders": 1,
        "transactions": 1,
        "debts": 1,
        "debt_payments": 1,
        "tasks": 1,
    }

    # Every section is present and holds Alice's data.
    assert document["profile"]["display_name"] == "Alice"
    assert document["profile"]["telegram"]["telegram_id"] == 111111
    assert document["profile"]["telegram"]["username"] == "alice_tg"
    assert document["settings"]["timezone"] == "Asia/Dhaka"
    assert document["settings"]["language"] == "en"

    assert [m["content"] for m in document["memories"]] == [
        "My cat is named Miso"
    ]
    assert [r["title"] for r in document["reminders"]] == [
        "Drink water"
    ]
    assert document["reminders"][0]["next_at"] is not None

    assert len(document["transactions"]) == 1
    assert document["transactions"][0]["amount"] == 250.0
    assert document["transactions"][0]["note"] == "lunch"

    assert len(document["debts"]) == 1
    debt = document["debts"][0]
    assert debt["counterparty"] == "Karim"
    assert debt["principal"] == 1000.0
    assert debt["remaining"] == 400.0
    assert len(debt["payments"]) == 1
    assert debt["payments"][0]["amount"] == 600.0

    assert [t["title"] for t in document["tasks"]] == ["Buy milk"]

    # Nothing of Bob's leaks into Alice's export.
    serialized = json.dumps(document)
    assert "Bob's private memory" not in serialized
    assert "bob feast" not in serialized
    assert "Bob's task" not in serialized
    assert "BobCounterparty" not in serialized
    assert "bob_tg" not in serialized
    assert "9999" not in serialized


def test_export_command_is_in_the_bot_menu():
    commands = [command.command for command in USER_BOT_COMMANDS]
    assert "export" in commands
