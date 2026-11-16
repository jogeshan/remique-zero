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
        "menu:help",
    ]


def test_every_menu_button_is_routable():
    keyboards = [
        menu.main_menu_keyboard(),
        menu.money_menu_keyboard(),
        menu.reminders_menu_keyboard(),
        menu.tasks_menu_keyboard(),
        menu.account_menu_keyboard(),
        menu.content_keyboard("money"),
    ]
    known = set(menu.NAV_SCREENS) | set(menu.ACTION_SECTIONS)
    for keyboard in keyboards:
        for data in _all_callback_data(keyboard):
            assert data in known, data
            assert len(data.encode("utf-8")) <= 64


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
