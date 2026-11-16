"""Tests for the interactive /menu (app/handlers/menu.py)."""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.handlers import menu
from app.services import finance_service, memory_service, reminder_service, task_service


def _all_buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def _all_callback_data(keyboard):
    return [button.callback_data for button in _all_buttons(keyboard)]


def test_main_menu_has_all_sections():
    data = _all_callback_data(menu.main_menu_keyboard())
    assert data == [
        "menu:money",
        "menu:reminders",
        "menu:tasks",
        "menu:account",
        "menu:quickadd",
        "menu:help",
    ]


def test_submenus_have_quick_add_buttons():
    assert "menu:add:expense" in _all_callback_data(menu.money_menu_keyboard())
    assert "menu:add:income" in _all_callback_data(menu.money_menu_keyboard())
    assert "menu:add:reminder" in _all_callback_data(
        menu.reminders_menu_keyboard()
    )
    assert "menu:add:task" in _all_callback_data(menu.tasks_menu_keyboard())
    quick = _all_callback_data(menu.quickadd_menu_keyboard())
    assert quick[:4] == [
        "menu:add:expense",
        "menu:add:income",
        "menu:add:reminder",
        "menu:add:task",
    ]


def test_every_menu_button_is_routable():
    keyboards = [
        menu.main_menu_keyboard(),
        menu.money_menu_keyboard(),
        menu.reminders_menu_keyboard(),
        menu.tasks_menu_keyboard(),
        menu.account_menu_keyboard(),
        menu.quickadd_menu_keyboard(),
        menu.quick_add_cancel_keyboard(),
        menu.content_keyboard("money"),
    ]
    known = set(menu.NAV_SCREENS) | set(menu.ACTION_SECTIONS)
    known |= {f"menu:add:{kind}" for kind in menu.QUICK_ADD_KINDS}
    known.add("menu:add:cancel")
    for keyboard in keyboards:
        for data in _all_callback_data(keyboard):
            assert data in known, data
            assert len(data.encode("utf-8")) <= 64


def test_parse_quick_add_callback():
    assert menu.parse_quick_add_callback("menu:add:expense") == "expense"
    assert menu.parse_quick_add_callback("menu:add:task") == "task"
    assert menu.parse_quick_add_callback("menu:add:cancel") == "cancel"
    assert menu.parse_quick_add_callback("menu:add:bogus") is None
    assert menu.parse_quick_add_callback("menu:money") is None
    assert menu.parse_quick_add_callback(None) is None


def test_action_sections_point_at_real_screens():
    for section in menu.ACTION_SECTIONS.values():
        assert f"menu:{section}" in menu.NAV_SCREENS


def test_menu_command_is_in_bot_commands():
    from app.bot import USER_BOT_COMMANDS

    names = [command.command for command in USER_BOT_COMMANDS]
    assert "menu" in names


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=42)
        self.edited = []
        self.answers = []

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edited.append((text, reply_markup))

    async def answer(self, text=None):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_nav_callback_edits_message_without_db():
    query = _FakeQuery("menu:money")
    update = SimpleNamespace(callback_query=query)

    await menu.menu_callback_handler(update, None)

    assert len(query.edited) == 1
    text, keyboard = query.edited[0]
    assert "Money" in text
    assert "menu:act:summary" in _all_callback_data(keyboard)


@pytest.mark.asyncio
async def test_unknown_callback_is_rejected():
    query = _FakeQuery("menu:nope")
    update = SimpleNamespace(callback_query=query)

    await menu.menu_callback_handler(update, None)

    assert query.edited == []
    assert query.answers == ["This button is no longer valid."]


@pytest.mark.asyncio
async def test_debts_text_empty(monkeypatch):
    async def fake_list(session, user_id):
        return []

    monkeypatch.setattr(finance_service, "list_debts", fake_list)
    text = await menu._debts_text(None, "user-id")
    assert "no open debts" in text


@pytest.mark.asyncio
async def test_debts_text_lists_both_directions(monkeypatch):
    debt = SimpleNamespace(
        direction="borrowed",
        counterparty="Rahim",
        remaining=1500,
        principal=2000,
    )
    loan = SimpleNamespace(
        direction="lent",
        counterparty="Karim",
        remaining=500,
        principal=500,
    )

    async def fake_list(session, user_id):
        return [debt, loan]

    async def fake_totals(session, user_id):
        return {"owed_by_user": 1500, "owed_to_user": 500, "open_count": 2}

    monkeypatch.setattr(finance_service, "list_debts", fake_list)
    monkeypatch.setattr(finance_service, "debt_totals", fake_totals)

    text = await menu._debts_text(None, "user-id")
    assert "Rahim" in text and "Karim" in text
    assert "You owe:" in text and "Owed to you:" in text


@pytest.mark.asyncio
async def test_reminders_text_empty(monkeypatch):
    async def fake_list(session, user_id):
        return []

    monkeypatch.setattr(reminder_service, "list_active_reminders", fake_list)
    text = await menu._reminders_text(None, "user-id", ZoneInfo("Asia/Dhaka"))
    assert "don't have any active reminders" in text


@pytest.mark.asyncio
async def test_tasks_text_caps_long_lists(monkeypatch):
    tasks = [
        SimpleNamespace(title=f"Task {i}", due_at=None) for i in range(12)
    ]

    async def fake_list(session, user_id, status="open"):
        return tasks

    monkeypatch.setattr(task_service, "list_tasks", fake_list)
    text = await menu._tasks_text(None, "user-id", ZoneInfo("Asia/Dhaka"))
    assert "Task 9" in text
    assert "Task 10" not in text
    assert "2 more" in text


@pytest.mark.asyncio
async def test_memories_text_empty(monkeypatch):
    async def fake_list(session, user_id):
        return []

    monkeypatch.setattr(memory_service, "list_memories", fake_list)
    text = await menu._memories_text(None, "user-id")
    assert "haven't saved any memories" in text


# --- quick add flow ---------------------------------------------------


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _quick_add_update(text):
    message = _FakeMessage(text)
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(type="private"),
    )
    return update, message


def _patch_account(monkeypatch):
    import uuid

    account = SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        menu,
        "get_session_factory",
        lambda: (lambda: _FakeSession()),
    )

    async def fake_resolve(session, telegram_user_id):
        return account

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )

    async def fake_tz(session, user_id):
        return "Asia/Dhaka"

    monkeypatch.setattr(menu, "_get_user_timezone_name", fake_tz)
    return account


@pytest.mark.asyncio
async def test_quick_add_button_sets_state_and_prompts():
    query = _FakeQuery("menu:add:expense")
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={})

    await menu.menu_callback_handler(update, context)

    assert context.user_data["quick_add"] == "expense"
    assert len(query.edited) == 1
    text, keyboard = query.edited[0]
    assert "Add expense" in text
    assert "menu:add:cancel" in _all_callback_data(keyboard)


@pytest.mark.asyncio
async def test_quick_add_cancel_button_clears_state():
    query = _FakeQuery("menu:add:cancel")
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={"quick_add": "task"})

    await menu.menu_callback_handler(update, context)

    assert "quick_add" not in context.user_data
    assert query.answers == ["Quick add cancelled."]


@pytest.mark.asyncio
async def test_quick_add_expense_success(monkeypatch):
    _patch_account(monkeypatch)
    saved = {}

    async def fake_add(session, user_id, **kwargs):
        saved.update(kwargs)

    async def fake_summarize(session, user_id, since=None, until=None):
        return {"total_expense": 250, "total_income": 0}

    monkeypatch.setattr(finance_service, "add_transaction", fake_add)
    monkeypatch.setattr(finance_service, "summarize", fake_summarize)

    update, message = _quick_add_update("250 lunch")
    context = SimpleNamespace(user_data={"quick_add": "expense"})

    handled = await menu.quick_add_message_handler(update, context)

    assert handled is True
    assert saved["type"] == "expense"
    assert saved["amount"] == 250
    assert saved["note"] == "lunch"
    assert "quick_add" not in context.user_data
    assert "Expense added" in message.replies[0]


@pytest.mark.asyncio
async def test_quick_add_bad_amount_keeps_state(monkeypatch):
    _patch_account(monkeypatch)

    update, message = _quick_add_update("lunch please")
    context = SimpleNamespace(user_data={"quick_add": "expense"})

    handled = await menu.quick_add_message_handler(update, context)

    assert handled is True
    assert context.user_data["quick_add"] == "expense"
    assert "amount first" in message.replies[0]


@pytest.mark.asyncio
async def test_quick_add_task_success(monkeypatch):
    _patch_account(monkeypatch)
    created = {}

    async def fake_create(session, user_id, title=None, **kwargs):
        created["title"] = title
        return SimpleNamespace(title=title)

    monkeypatch.setattr(task_service, "create_task", fake_create)

    update, message = _quick_add_update("book the train tickets")
    context = SimpleNamespace(user_data={"quick_add": "task"})

    handled = await menu.quick_add_message_handler(update, context)

    assert handled is True
    assert created["title"] == "book the train tickets"
    assert "quick_add" not in context.user_data
    assert "Task added" in message.replies[0]


@pytest.mark.asyncio
async def test_quick_add_reminder_without_time_keeps_state(monkeypatch):
    _patch_account(monkeypatch)
    monkeypatch.setattr(
        reminder_service,
        "parse_reminder_text",
        lambda text, tz, now: None,
    )

    update, message = _quick_add_update("call Rahim")
    context = SimpleNamespace(user_data={"quick_add": "reminder"})

    handled = await menu.quick_add_message_handler(update, context)

    assert handled is True
    assert context.user_data["quick_add"] == "reminder"
    assert "date or time" in message.replies[0]


@pytest.mark.asyncio
async def test_no_pending_quick_add_passes_through():
    update, _message = _quick_add_update("hello there")
    context = SimpleNamespace(user_data={})

    handled = await menu.quick_add_message_handler(update, context)

    assert handled is False


@pytest.mark.asyncio
async def test_cancel_command_clears_state():
    message = _FakeMessage("/cancel")
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(user_data={"quick_add": "income"})

    await menu.cancel_command(update, context)

    assert "quick_add" not in context.user_data
    assert message.replies == ["Quick add cancelled."]
