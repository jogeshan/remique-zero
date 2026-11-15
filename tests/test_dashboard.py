"""Tests for Phase 11: the web API + dashboard.

Covers the API token service (only the SHA-256 hash is stored,
rotation revokes the previous token, authentication rejects
unknown/revoked tokens) and the /api/v1 endpoints through the
FastAPI TestClient with the DB session dependency overridden by
an in-memory fake session — the same fake-session style as the
rest of the suite (aiosqlite is not available in the test
environment). Scoping is asserted throughout: a token only ever
returns its own user's data.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    UnaryExpression,
)

from app.database import get_db_session
from app.main import app
from app.models import (
    ApiToken,
    Debt,
    Reminder,
    ReminderOccurrence,
    Task,
    TelegramAccount,
    Transaction,
    User,
    UserMemory,
    UserSetting,
)
from app.services import api_token_service, finance_service

DHAKA = ZoneInfo("Asia/Dhaka")
NOW_UTC = datetime.now(timezone.utc)
TODAY_NOON = datetime.now(DHAKA).replace(
    hour=12, minute=0, second=0, microsecond=0
)
YESTERDAY_NOON = TODAY_NOON - timedelta(days=1)


# --- fake session ---------------------------------------------------------


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
    """
    In-memory AsyncSession stand-in.

    Evaluates the simple SELECT shapes the dashboard code issues:
    equality / range / IS NULL filters, ORDER BY, LIMIT, and
    COUNT aggregates. `add()` files objects under their table.
    """

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

    # -- query evaluation ------------------------------------------------

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
            # Single-column select, e.g. next occurrence time.
            rows = self._filtered(expr.table.name, query)
            rows = self._ordered(rows, query)
            limit = query._limit_clause
            if limit is not None:
                rows = rows[: limit.value]
            return [getattr(row, expr.key) for row in rows]

        # Aggregate select (COUNT): count the filtered FROM rows.
        table_name = query.get_final_froms()[0].name
        return [len(self._filtered(table_name, query))]


# --- fixture data -----------------------------------------------------------


def _user(role="user", name="Alice"):
    return User(
        id=uuid.uuid4(),
        display_name=name,
        status="active",
        role=role,
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )


def _account(user, telegram_id, username):
    return TelegramAccount(
        user_id=user.id,
        telegram_user_id=telegram_id,
        chat_id=telegram_id,
        username=username,
        linked_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )


def _setting(user):
    return UserSetting(
        user_id=user.id,
        timezone="Asia/Dhaka",
        language="en",
    )


def _token_row(user, raw_token, label="dashboard"):
    return ApiToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=api_token_service.hash_token(raw_token),
        label=label,
        created_at=NOW_UTC,
    )


def _transaction(user, type, amount, category, occurred_at, note=None):
    return Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        type=type,
        amount=Decimal(str(amount)),
        currency="BDT",
        category=category,
        note=note,
        occurred_at=occurred_at,
        created_at=occurred_at,
        updated_at=occurred_at,
    )


ALICE_TOKEN = "rz_alice_test_token_0123456789abcdef"
BOB_TOKEN = "rz_bob_test_token_0123456789abcdef01"
ADMIN_TOKEN = "rz_admin_test_token_0123456789abcdef"


def build_session():
    """A fake session with two users, one admin, and scoped data."""
    alice = _user(name="Alice")
    bob = _user(name="Bob")
    admin = _user(role="admin", name="Admin")

    session = FakeSession()
    session.seed(
        alice,
        bob,
        admin,
        _account(alice, 111111, "alice_tg"),
        _account(bob, 222222, "bob_tg"),
        _account(admin, 999999, "admin_tg"),
        _setting(alice),
        _setting(bob),
        _setting(admin),
        _token_row(alice, ALICE_TOKEN),
        _token_row(bob, BOB_TOKEN),
        _token_row(admin, ADMIN_TOKEN),
        # Alice: today + yesterday activity.
        _transaction(alice, "expense", 250, "Food", TODAY_NOON, "lunch"),
        _transaction(
            alice, "expense", 100, "Transport", YESTERDAY_NOON, "rickshaw"
        ),
        _transaction(
            alice, "income", 5000, "Salary", YESTERDAY_NOON, "payday"
        ),
        # Bob: one big expense today.
        _transaction(bob, "expense", 9999, "Food", TODAY_NOON, "feast"),
    )

    debt_open = Debt(
        id=uuid.uuid4(),
        user_id=alice.id,
        direction="borrowed",
        kind="debt",
        counterparty="Karim",
        principal=Decimal("1000"),
        remaining=Decimal("400"),
        status="open",
        created_at=TODAY_NOON,
        updated_at=TODAY_NOON,
    )
    debt_settled = Debt(
        id=uuid.uuid4(),
        user_id=alice.id,
        direction="lent",
        kind="loan",
        counterparty="Rahim",
        principal=Decimal("500"),
        remaining=Decimal("0"),
        status="settled",
        created_at=YESTERDAY_NOON,
        updated_at=YESTERDAY_NOON,
    )
    bob_debt = Debt(
        id=uuid.uuid4(),
        user_id=bob.id,
        direction="lent",
        kind="debt",
        counterparty="Someone",
        principal=Decimal("777"),
        remaining=Decimal("777"),
        status="open",
        created_at=TODAY_NOON,
        updated_at=TODAY_NOON,
    )
    session.seed(debt_open, debt_settled, bob_debt)

    reminder = Reminder(
        id=uuid.uuid4(),
        user_id=alice.id,
        title="Drink water",
        timezone="Asia/Dhaka",
        status="active",
        created_at=TODAY_NOON,
        updated_at=TODAY_NOON,
    )
    cancelled = Reminder(
        id=uuid.uuid4(),
        user_id=alice.id,
        title="Old reminder",
        timezone="Asia/Dhaka",
        status="cancelled",
        created_at=TODAY_NOON,
        updated_at=TODAY_NOON,
    )
    next_at = datetime.now(timezone.utc) + timedelta(hours=2)
    occurrence = ReminderOccurrence(
        id=uuid.uuid4(),
        reminder_id=reminder.id,
        scheduled_for=next_at,
        status="scheduled",
        created_at=TODAY_NOON,
    )
    session.seed(reminder, cancelled, occurrence)

    session.seed(
        Task(
            id=uuid.uuid4(),
            user_id=alice.id,
            title="Buy milk",
            status="open",
            created_at=TODAY_NOON,
            updated_at=TODAY_NOON,
        ),
        Task(
            id=uuid.uuid4(),
            user_id=alice.id,
            title="File taxes",
            status="done",
            completed_at=TODAY_NOON,
            created_at=YESTERDAY_NOON,
            updated_at=TODAY_NOON,
        ),
        Task(
            id=uuid.uuid4(),
            user_id=bob.id,
            title="Bob's task",
            status="open",
            created_at=TODAY_NOON,
            updated_at=TODAY_NOON,
        ),
        UserMemory(
            id=uuid.uuid4(),
            user_id=alice.id,
            content="My cat is named Miso",
            category="fact",
            created_at=TODAY_NOON,
            updated_at=TODAY_NOON,
        ),
        UserMemory(
            id=uuid.uuid4(),
            user_id=alice.id,
            content="Prefers tea over coffee",
            category="preference",
            created_at=YESTERDAY_NOON,
            updated_at=YESTERDAY_NOON,
        ),
        UserMemory(
            id=uuid.uuid4(),
            user_id=bob.id,
            content="Bob's secret memory",
            category="fact",
            created_at=TODAY_NOON,
            updated_at=TODAY_NOON,
        ),
    )

    return session, alice, bob, admin


@pytest.fixture()
def client_and_session():
    session, alice, bob, admin = build_session()

    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    try:
        yield TestClient(app), session, alice, bob, admin
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def _auth(raw_token):
    return {"Authorization": f"Bearer {raw_token}"}


# --- token service ----------------------------------------------------------


async def test_create_token_stores_only_the_hash():
    session = FakeSession()
    user = _user()
    session.seed(user)

    token, raw = await api_token_service.create_token(session, user.id)

    assert raw.startswith(api_token_service.TOKEN_PREFIX)
    assert token.token_hash == api_token_service.hash_token(raw)
    assert token.token_hash != raw
    assert raw not in token.token_hash
    # The raw token is not stored in any column of the row.
    for value in vars(token).values():
        assert value != raw
    assert session.committed


async def test_create_token_rotates_previous_tokens():
    session = FakeSession()
    user = _user()
    session.seed(user)

    first, first_raw = await api_token_service.create_token(
        session, user.id
    )
    second, second_raw = await api_token_service.create_token(
        session, user.id
    )

    assert first.revoked_at is not None
    assert second.revoked_at is None

    # The rotated-out token no longer authenticates.
    assert (
        await api_token_service.authenticate_token(session, first_raw)
        is None
    )

    resolved = await api_token_service.authenticate_token(
        session, second_raw
    )
    assert resolved is not None
    assert resolved.id == user.id


async def test_authenticate_rejects_unknown_revoked_and_inactive():
    session = FakeSession()
    user = _user()
    session.seed(user)
    token, raw = await api_token_service.create_token(session, user.id)

    assert (
        await api_token_service.authenticate_token(session, "rz_nope")
        is None
    )
    assert (
        await api_token_service.authenticate_token(session, "")
        is None
    )

    token.revoked_at = datetime.now(timezone.utc)
    assert (
        await api_token_service.authenticate_token(session, raw) is None
    )

    token.revoked_at = None
    user.status = "disabled"
    assert (
        await api_token_service.authenticate_token(session, raw) is None
    )


async def test_authenticate_updates_last_used_at():
    session = FakeSession()
    user = _user()
    session.seed(user)
    token, raw = await api_token_service.create_token(session, user.id)

    assert token.last_used_at is None
    resolved = await api_token_service.authenticate_token(session, raw)
    assert resolved is not None
    assert token.last_used_at is not None


# --- dashboard page -----------------------------------------------------------


def test_dashboard_page_loads_without_auth(client_and_session):
    client, *_ = client_and_session

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="token"' in response.text
    assert "/api/v1/summary" in response.text


# --- API authentication -------------------------------------------------------


def test_api_rejects_missing_and_wrong_tokens(client_and_session):
    client, *_ = client_and_session

    assert client.get("/api/v1/me").status_code == 401
    assert (
        client.get("/api/v1/me", headers=_auth("rz_wrong")).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/me", headers={"Authorization": ALICE_TOKEN}
        ).status_code
        == 401
    )


def test_api_rejects_revoked_token(client_and_session):
    client, session, alice, *_ = client_and_session

    token = session.tables["api_tokens"][0]
    assert token.user_id == alice.id
    token.revoked_at = datetime.now(timezone.utc)

    response = client.get("/api/v1/me", headers=_auth(ALICE_TOKEN))
    assert response.status_code == 401


# --- endpoint scoping -----------------------------------------------------------


def test_me_returns_own_profile(client_and_session):
    client, *_ = client_and_session

    response = client.get("/api/v1/me", headers=_auth(ALICE_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["telegram_id"] == 111111
    assert body["username"] == "alice_tg"
    assert body["display_name"] == "Alice"
    assert body["role"] == "user"
    assert body["timezone"] == "Asia/Dhaka"
    assert body["language"] == "en"
    assert body["member_since"].startswith("2026-01-05")

    bob_body = client.get(
        "/api/v1/me", headers=_auth(BOB_TOKEN)
    ).json()
    assert bob_body["telegram_id"] == 222222
    assert bob_body["username"] == "bob_tg"


def test_summary_is_scoped_to_the_token_user(client_and_session):
    client, *_ = client_and_session

    body = client.get(
        "/api/v1/summary", headers=_auth(ALICE_TOKEN)
    ).json()

    # Yesterday is exact regardless of the day of the month.
    assert body["yesterday"]["total_expense"] == 100.0
    assert body["yesterday"]["total_income"] == 5000.0

    # The month figures cover Alice's rows only — never Bob's 9999.
    month_since, month_until = finance_service.period_range(
        "month", DHAKA, datetime.now(timezone.utc)
    )

    def in_month(moment):
        return month_since <= moment < month_until

    expected_expense = (250.0 if in_month(TODAY_NOON) else 0.0) + (
        100.0 if in_month(YESTERDAY_NOON) else 0.0
    )
    expected_income = 5000.0 if in_month(YESTERDAY_NOON) else 0.0
    assert body["month"]["total_expense"] == expected_expense
    assert body["month"]["total_income"] == expected_income

    assert body["debts"]["owed_by_user"] == 400.0
    assert body["debts"]["open_count"] == 1

    bob_body = client.get(
        "/api/v1/summary", headers=_auth(BOB_TOKEN)
    ).json()
    assert bob_body["month"]["total_expense"] == 9999.0
    assert bob_body["yesterday"]["total_expense"] == 0.0


def test_transactions_are_scoped(client_and_session):
    client, *_ = client_and_session

    body = client.get(
        "/api/v1/transactions", headers=_auth(ALICE_TOKEN)
    ).json()
    amounts = sorted(t["amount"] for t in body["transactions"])
    assert amounts == [100.0, 250.0, 5000.0]
    assert all(t["currency"] == "BDT" for t in body["transactions"])

    bob_body = client.get(
        "/api/v1/transactions", headers=_auth(BOB_TOKEN)
    ).json()
    assert [t["amount"] for t in bob_body["transactions"]] == [9999.0]

    limited = client.get(
        "/api/v1/transactions?limit=1", headers=_auth(ALICE_TOKEN)
    ).json()
    assert len(limited["transactions"]) == 1
    # Newest first: today's lunch.
    assert limited["transactions"][0]["amount"] == 250.0


def test_debts_reminders_tasks_memories_are_scoped(client_and_session):
    client, *_ = client_and_session
    headers = _auth(ALICE_TOKEN)

    debts = client.get("/api/v1/debts", headers=headers).json()
    assert [d["counterparty"] for d in debts["open"]] == ["Karim"]
    assert debts["open"][0]["remaining"] == 400.0
    assert [d["counterparty"] for d in debts["settled"]] == ["Rahim"]
    assert debts["totals"]["owed_by_user"] == 400.0

    reminders = client.get("/api/v1/reminders", headers=headers).json()
    assert [r["title"] for r in reminders["reminders"]] == [
        "Drink water"
    ]
    assert reminders["reminders"][0]["next_at"] is not None

    tasks = client.get("/api/v1/tasks", headers=headers).json()
    assert [t["title"] for t in tasks["open"]] == ["Buy milk"]
    assert [t["title"] for t in tasks["recently_completed"]] == [
        "File taxes"
    ]

    memories = client.get("/api/v1/memories", headers=headers).json()
    contents = {m["content"] for m in memories["memories"]}
    assert contents == {
        "My cat is named Miso",
        "Prefers tea over coffee",
    }


# --- admin stats -----------------------------------------------------------


def test_admin_stats_forbidden_for_regular_user(client_and_session):
    client, *_ = client_and_session

    response = client.get(
        "/api/v1/admin/stats", headers=_auth(ALICE_TOKEN)
    )
    assert response.status_code == 403


def test_admin_stats_returns_counts_for_admin(client_and_session):
    client, *_ = client_and_session

    response = client.get(
        "/api/v1/admin/stats", headers=_auth(ADMIN_TOKEN)
    )
    assert response.status_code == 200
    body = response.json()

    assert body["users"] == 3
    assert body["active_reminders"] == 1
    assert body["open_tasks"] == 2

    month_since, month_until = finance_service.period_range(
        "month", DHAKA, datetime.now(timezone.utc)
    )
    expected = sum(
        1
        for moment in (TODAY_NOON, YESTERDAY_NOON, YESTERDAY_NOON, TODAY_NOON)
        if month_since <= moment < month_until
    )
    assert body["transactions_this_month"] == expected

    # No personal content leaks into the stats payload.
    assert "counterparty" not in response.text
    assert "Karim" not in response.text
