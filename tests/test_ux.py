"""
Phase 10 (Telegram UX) tests: long-message splitting, the bot
command menu, reminder action buttons (keyboard, callback
parsing, ownership, snooze semantics) and /settings.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import bot as bot_module
from app.handlers import callbacks
from app.handlers.text_utils import reply_long, split_message
from app.models import DeliveryJob, Reminder, ReminderOccurrence, UserSetting
from app.services import ai_service, delivery_service, memory_service, reminder_service


# --- split_message ----------------------------------------------------


def test_split_short_text_unchanged():
    assert split_message("hello") == ["hello"]
    assert split_message("") == [""]
    assert split_message("x" * 4096) == ["x" * 4096]


def test_split_prefers_paragraph_boundary():
    text = "intro paragraph\n\n" + "b" * 5000
    chunks = split_message(text, limit=4096)

    assert chunks[0] == "intro paragraph"
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_split_prefers_line_boundary():
    text = "first line\n" + "c" * 5000
    chunks = split_message(text, limit=4096)

    assert chunks[0] == "first line"
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_split_on_word_boundaries_keeps_words():
    text = "word " * 3000
    chunks = split_message(text, limit=4096)

    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert sum(chunk.count("word") for chunk in chunks) == 3000


def test_split_hard_cut_as_last_resort():
    text = "x" * 9000
    chunks = split_message(text, limit=4096)

    assert [len(chunk) for chunk in chunks] == [4096, 4096, 808]
    assert "".join(chunks) == text


async def test_reply_long_sends_each_chunk():
    sent = []

    class _Message:
        async def reply_text(self, text):
            sent.append(text)

    await reply_long(_Message(), "x" * 9000)

    assert len(sent) == 3
    assert all(len(chunk) <= 4096 for chunk in sent)


# --- bot command menu ---------------------------------------------------


async def test_setup_bot_commands_registers_user_menu():
    class _MenuBot:
        def __init__(self):
            self.commands = None

        async def set_my_commands(self, commands):
            self.commands = commands

    application = SimpleNamespace(bot=_MenuBot())

    await bot_module.setup_bot_commands(application)

    names = [command.command for command in application.bot.commands]
    assert names == [
        "start",
        "help",
        "newchat",
        "memories",
        "remind",
        "reminders",
        "expense",
        "income",
        "summary",
        "debts",
        "tasks",
        "settings",
    ]


async def test_setup_bot_commands_swallows_failures():
    class _BrokenBot:
        async def set_my_commands(self, commands):
            raise RuntimeError("telegram is down")

    application = SimpleNamespace(bot=_BrokenBot())

    # Must not raise: the menu never blocks startup.
    await bot_module.setup_bot_commands(application)


# --- reminder keyboard + callback parsing -------------------------------


def test_reminder_keyboard_callback_data():
    occurrence_id = uuid.uuid4()
    keyboard = delivery_service.reminder_keyboard(occurrence_id)

    buttons = keyboard.inline_keyboard[0]
    data = [button.callback_data for button in buttons]

    assert data == [
        f"rem:done:{occurrence_id}",
        f"rem:snooze60:{occurrence_id}",
        f"rem:snooze1440:{occurrence_id}",
    ]
    assert all(len(item.encode()) <= 64 for item in data)


def test_reminder_callback_parse_round_trip():
    occurrence_id = uuid.uuid4()

    for action in ("done", "snooze60", "snooze1440"):
        parsed = callbacks.parse_reminder_callback(
            f"rem:{action}:{occurrence_id}"
        )
        assert parsed == (action, occurrence_id)


def test_reminder_callback_parse_rejects_garbage():
    occurrence_id = uuid.uuid4()

    assert callbacks.parse_reminder_callback(None) is None
    assert callbacks.parse_reminder_callback("") is None
    assert callbacks.parse_reminder_callback("rem:") is None
    assert callbacks.parse_reminder_callback("rem:done") is None
    assert (
        callbacks.parse_reminder_callback(f"rem:explode:{occurrence_id}")
        is None
    )
    assert callbacks.parse_reminder_callback("rem:done:not-a-uuid") is None
    assert (
        callbacks.parse_reminder_callback(f"set:tz:UTC") is None
    )


def test_settings_callback_parse():
    assert callbacks.parse_settings_callback("set:tz:Asia/Dhaka") == (
        "tz",
        "Asia/Dhaka",
    )
    assert callbacks.parse_settings_callback("set:tz:UTC") == ("tz", "UTC")
    assert callbacks.parse_settings_callback("set:lang:bn") == ("lang", "bn")
    assert callbacks.parse_settings_callback("set:tz:Mars/Olympus") is None
    assert callbacks.parse_settings_callback("set:lang:fr") is None
    assert callbacks.parse_settings_callback("set:other:x") is None
    assert callbacks.parse_settings_callback(None) is None


# --- reminder callback handler ------------------------------------------


class _FakeQuery:
    def __init__(self, data, telegram_user_id=42):
        self.data = data
        self.from_user = SimpleNamespace(id=telegram_user_id)
        self.message = SimpleNamespace(text="⏰ Reminder: Pay rent")
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text=None, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


class _CallbackSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_callback_env(monkeypatch, *, user_id, pair):
    monkeypatch.setattr(
        callbacks,
        "get_session_factory",
        lambda: (lambda: _CallbackSession()),
    )

    async def fake_resolve(session, telegram_user_id):
        return SimpleNamespace(id=user_id)

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )

    async def fake_pair(session, occurrence_id):
        return pair

    monkeypatch.setattr(
        reminder_service,
        "get_reminder_occurrence",
        fake_pair,
    )


async def test_reminder_callback_rejects_non_owner(monkeypatch):
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    occurrence_id = uuid.uuid4()

    pair = (
        SimpleNamespace(id=occurrence_id, status="delivered"),
        SimpleNamespace(id=uuid.uuid4(), user_id=other_id),
    )
    _patch_callback_env(monkeypatch, user_id=owner_id, pair=pair)

    calls = []

    async def fake_done(session, user_id, occurrence_id):
        calls.append("done")
        return True

    async def fake_snooze(session, user_id, occurrence_id, minutes):
        calls.append("snooze")
        return True

    monkeypatch.setattr(reminder_service, "mark_occurrence_done", fake_done)
    monkeypatch.setattr(
        reminder_service,
        "snooze_delivered_occurrence",
        fake_snooze,
    )

    query = _FakeQuery(f"rem:done:{occurrence_id}")
    update = SimpleNamespace(callback_query=query)

    await callbacks.reminder_callback_handler(update, SimpleNamespace())

    assert query.answers == ["This isn't your reminder."]
    assert query.edits == []
    assert calls == []


async def test_reminder_callback_done_for_owner(monkeypatch):
    owner_id = uuid.uuid4()
    occurrence_id = uuid.uuid4()

    pair = (
        SimpleNamespace(id=occurrence_id, status="delivered"),
        SimpleNamespace(id=uuid.uuid4(), user_id=owner_id),
    )
    _patch_callback_env(monkeypatch, user_id=owner_id, pair=pair)

    calls = []

    async def fake_done(session, user_id, occurrence_id):
        calls.append((user_id, occurrence_id))
        return True

    monkeypatch.setattr(reminder_service, "mark_occurrence_done", fake_done)

    query = _FakeQuery(f"rem:done:{occurrence_id}")
    update = SimpleNamespace(callback_query=query)

    await callbacks.reminder_callback_handler(update, SimpleNamespace())

    assert calls == [(owner_id, occurrence_id)]
    assert query.answers == ["✅ Done"]
    assert len(query.edits) == 1
    text, markup = query.edits[0]
    assert text.endswith("✅ Done")
    assert markup is None


async def test_reminder_callback_unknown_occurrence(monkeypatch):
    _patch_callback_env(
        monkeypatch,
        user_id=uuid.uuid4(),
        pair=None,
    )

    query = _FakeQuery(f"rem:done:{uuid.uuid4()}")
    update = SimpleNamespace(callback_query=query)

    await callbacks.reminder_callback_handler(update, SimpleNamespace())

    assert query.answers == ["That reminder is no longer available."]
    assert query.edits == []


# --- reminder_service button operations ---------------------------------


class _ButtonSession:
    """Fake session backed by an entity store, like the delivery fakes."""

    def __init__(self, entities, job=None):
        self.entities = entities
        self.job = job
        self.added = []
        self.commits = 0

    async def get(self, model, pk):
        return self.entities.get((model.__name__, pk))

    async def scalar(self, *args, **kwargs):
        return self.job

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


def _occurrence_and_reminder(
    *,
    user_id,
    occurrence_status="delivered",
    reminder_status="completed",
    recurrence_rule=None,
    scheduled_for=None,
):
    reminder_id = uuid.uuid4()
    occurrence_id = uuid.uuid4()

    reminder = SimpleNamespace(
        id=reminder_id,
        user_id=user_id,
        status=reminder_status,
        recurrence_rule=recurrence_rule,
    )
    occurrence = SimpleNamespace(
        id=occurrence_id,
        reminder_id=reminder_id,
        status=occurrence_status,
        scheduled_for=scheduled_for
        or datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc),
        completed_at=None,
    )
    entities = {
        (ReminderOccurrence.__name__, occurrence_id): occurrence,
        (Reminder.__name__, reminder_id): reminder,
    }
    return entities, occurrence, reminder


async def test_mark_occurrence_done_one_time():
    user_id = uuid.uuid4()
    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=user_id,
        reminder_status="active",
    )
    session = _ButtonSession(entities)

    result = await reminder_service.mark_occurrence_done(
        session,
        user_id,
        occurrence.id,
    )

    assert result is True
    assert occurrence.status == "done"
    assert occurrence.completed_at is not None
    assert reminder.status == "completed"


async def test_mark_occurrence_done_recurring_keeps_reminder_active():
    user_id = uuid.uuid4()
    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=user_id,
        reminder_status="active",
        recurrence_rule="FREQ=DAILY",
    )
    session = _ButtonSession(entities)

    result = await reminder_service.mark_occurrence_done(
        session,
        user_id,
        occurrence.id,
    )

    assert result is True
    assert occurrence.status == "done"
    assert reminder.status == "active"


async def test_mark_occurrence_done_rejects_other_user():
    user_id = uuid.uuid4()
    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=uuid.uuid4(),
    )
    session = _ButtonSession(entities)

    result = await reminder_service.mark_occurrence_done(
        session,
        user_id,
        occurrence.id,
    )

    assert result is False
    assert occurrence.status == "delivered"


async def test_snooze_delivered_one_time_recreates_occurrence():
    user_id = uuid.uuid4()
    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=user_id,
        reminder_status="completed",
    )
    session = _ButtonSession(entities)
    now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)

    result = await reminder_service.snooze_delivered_occurrence(
        session,
        user_id,
        occurrence.id,
        60,
        now=now,
    )

    assert result is True
    assert reminder.status == "active"

    new_occurrences = [
        obj for obj in session.added if isinstance(obj, ReminderOccurrence)
    ]
    new_jobs = [
        obj for obj in session.added if isinstance(obj, DeliveryJob)
    ]
    assert len(new_occurrences) == 1
    assert len(new_jobs) == 1

    new_occurrence = new_occurrences[0]
    assert new_occurrence.status == "scheduled"
    assert new_occurrence.scheduled_for == now + timedelta(minutes=60)

    job = new_jobs[0]
    assert job.occurrence_id == new_occurrence.id
    assert job.run_at == now + timedelta(minutes=60)
    assert job.job_type == "reminder_snooze"
    assert job.idempotency_key == f"reminder:{new_occurrence.id}"


async def test_snooze_delivered_recurring_adds_one_off():
    user_id = uuid.uuid4()
    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=user_id,
        reminder_status="active",
        recurrence_rule="FREQ=DAILY",
    )
    session = _ButtonSession(entities)
    now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)

    result = await reminder_service.snooze_delivered_occurrence(
        session,
        user_id,
        occurrence.id,
        1440,
        now=now,
    )

    assert result is True
    assert reminder.status == "active"
    new_occurrences = [
        obj for obj in session.added if isinstance(obj, ReminderOccurrence)
    ]
    assert len(new_occurrences) == 1
    assert new_occurrences[0].scheduled_for == now + timedelta(days=1)


async def test_snooze_scheduled_occurrence_moves_it():
    user_id = uuid.uuid4()
    start = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=user_id,
        occurrence_status="scheduled",
        reminder_status="active",
        scheduled_for=start,
    )
    job = SimpleNamespace(
        occurrence_id=occurrence.id,
        status="pending",
        run_at=start,
    )
    session = _ButtonSession(entities, job=job)

    result = await reminder_service.snooze_delivered_occurrence(
        session,
        user_id,
        occurrence.id,
        60,
    )

    assert result is True
    assert occurrence.scheduled_for == start + timedelta(minutes=60)
    assert job.run_at == start + timedelta(minutes=60)
    assert session.added == []


async def test_snooze_rejects_done_occurrence_and_other_user():
    user_id = uuid.uuid4()
    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=user_id,
        occurrence_status="done",
        reminder_status="active",
    )
    session = _ButtonSession(entities)

    assert (
        await reminder_service.snooze_delivered_occurrence(
            session,
            user_id,
            occurrence.id,
            60,
        )
        is False
    )

    entities, occurrence, reminder = _occurrence_and_reminder(
        user_id=uuid.uuid4(),
    )
    session = _ButtonSession(entities)

    assert (
        await reminder_service.snooze_delivered_occurrence(
            session,
            user_id,
            occurrence.id,
            60,
        )
        is False
    )
    assert session.added == []


# --- delivery attaches the keyboard --------------------------------------


class _DeliverySession:
    """Fake session backed by a shared store dict (delivery-test pattern)."""

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
        pass


class _MarkupBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=99)


async def test_delivery_attaches_reminder_keyboard():
    job_id = uuid.uuid4()
    occurrence_id = uuid.uuid4()
    reminder_id = uuid.uuid4()

    job = SimpleNamespace(
        id=job_id,
        occurrence_id=occurrence_id,
        job_type="reminder",
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
        user_id=uuid.uuid4(),
        title="Pay rent",
        description=None,
        status="active",
        recurrence_rule=None,
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

    bot = _MarkupBot()
    summary = await delivery_service.process_due_deliveries(
        lambda: _DeliverySession(store),
        bot,
        now=datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc),
    )

    assert summary["succeeded"] == 1
    assert len(bot.sent) == 1
    _, _, markup = bot.sent[0]
    assert markup is not None
    data = [
        button.callback_data
        for button in markup.inline_keyboard[0]
    ]
    assert data[0] == f"rem:done:{occurrence_id}"


# --- settings ------------------------------------------------------------


class _SettingsSession:
    def __init__(self, row):
        self.row = row
        self.added = []
        self.commits = 0
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, model, pk):
        self.get_calls.append((model, pk))
        return self.row

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


async def test_settings_callback_updates_own_row(monkeypatch):
    user_id = uuid.uuid4()
    row = SimpleNamespace(
        user_id=user_id,
        timezone="Asia/Dhaka",
        language="en",
    )
    session = _SettingsSession(row)

    monkeypatch.setattr(
        callbacks,
        "get_session_factory",
        lambda: (lambda: session),
    )

    async def fake_resolve(session_arg, telegram_user_id):
        assert telegram_user_id == 42
        return SimpleNamespace(id=user_id)

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )

    query = _FakeQuery("set:tz:Asia/Kolkata")
    update = SimpleNamespace(callback_query=query)

    await callbacks.settings_callback_handler(update, SimpleNamespace())

    # The row loaded was the clicking user's own, and it changed.
    assert session.get_calls == [(UserSetting, user_id)]
    assert row.timezone == "Asia/Kolkata"
    assert row.language == "en"
    assert session.commits == 1
    assert query.answers == ["Timezone set to Asia/Kolkata."]
    assert len(query.edits) == 1
    text, markup = query.edits[0]
    assert "Asia/Kolkata" in text
    assert markup is not None


async def test_settings_callback_creates_row_when_missing(monkeypatch):
    user_id = uuid.uuid4()
    session = _SettingsSession(None)

    monkeypatch.setattr(
        callbacks,
        "get_session_factory",
        lambda: (lambda: session),
    )

    async def fake_resolve(session_arg, telegram_user_id):
        return SimpleNamespace(id=user_id)

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )

    query = _FakeQuery("set:lang:bn")
    update = SimpleNamespace(callback_query=query)

    await callbacks.settings_callback_handler(update, SimpleNamespace())

    assert len(session.added) == 1
    created = session.added[0]
    assert isinstance(created, UserSetting)
    assert created.user_id == user_id
    assert created.language == "bn"
    assert query.answers == ["Language set to বাংলা (Bangla)."]


def test_settings_keyboard_marks_current_values():
    keyboard = callbacks.settings_keyboard("Europe/London", "bn")
    buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
    ]
    labels = {button.callback_data: button.text for button in buttons}

    assert labels["set:tz:Europe/London"].startswith("✓")
    assert not labels["set:tz:UTC"].startswith("✓")
    assert labels["set:lang:bn"].startswith("✓")
    assert not labels["set:lang:en"].startswith("✓")
    assert all(
        len(data.encode()) <= 64 for data in labels
    )


# --- system instruction language -----------------------------------------


def test_system_instruction_includes_preferred_language():
    instruction = ai_service.build_system_instruction(
        "user",
        language="bn",
    )
    assert "preferred language is Bangla (Bengali)" in instruction


def test_system_instruction_omits_default_language():
    for language in (None, "en"):
        instruction = ai_service.build_system_instruction(
            "user",
            language=language,
        )
        assert "preferred language" not in instruction
