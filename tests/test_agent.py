"""Tests for Phase 9: personal task lists and conversation
context compression (rolling summary).

Covers task_service at the fake-session level (create/list/
complete/delete, short-ID resolution including the ambiguous
prefix error), the AI tool dispatch for the task tools, the
pure compression helpers (threshold, summary prompt, cap), the
compression pass with a mocked Gemini client, and a full
generate_ai_reply flow proving an existing summary lands in
the system instruction while summarized messages stay out of
the contents. Real-DB behaviour is left to the integration
suite (aiosqlite is not available in the test environment).
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from google.genai import types

from app.models import Task
from app.services import (
    ai_service,
    ai_tools,
    conversation_service,
    memory_service,
    task_service,
)


# ---------------------------------------------------------------------------
# Task service (fake session)
# ---------------------------------------------------------------------------


class _TaskSession:
    """
    Fake session for task_service.

    scalars() returns the preset task list (prefix matching is
    done by the service in Python, so this exercises the real
    resolution logic); scalar() returns the preset single task
    used by full-UUID lookups. Status filtering itself is in the
    SQL query and is covered by query construction.
    """

    def __init__(self, tasks=(), scalar_task=None):
        self._tasks = list(tasks)
        self._scalar_task = scalar_task
        self.added = []
        self.deleted = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def scalars(self, *args, **kwargs):
        return list(self._tasks)

    async def scalar(self, *args, **kwargs):
        return self._scalar_task

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


def _task(task_id, title="Task", status="open", user_id=None):
    return Task(
        id=task_id,
        user_id=user_id or uuid.uuid4(),
        title=title,
        status=status,
    )


async def test_create_task_normalizes_and_defaults():
    session = _TaskSession()
    user_id = uuid.uuid4()

    task = await task_service.create_task(
        session,
        user_id,
        title="  Book the train tickets  ",
        notes="  Window seat if possible  ",
        due_at=datetime(2026, 11, 20, 9, 0),  # naive -> UTC
    )

    assert task.user_id == user_id
    assert task.title == "Book the train tickets"
    assert task.notes == "Window seat if possible"
    assert task.status == "open"
    assert task.due_at.tzinfo is not None
    assert session.added == [task]
    assert session.committed is True


async def test_create_task_rejects_empty_title():
    session = _TaskSession()

    for bad_title in ("", "   ", None):
        with pytest.raises(ValueError):
            await task_service.create_task(
                session,
                uuid.uuid4(),
                title=bad_title,
            )

    assert session.added == []


async def test_list_tasks_rejects_bad_status():
    session = _TaskSession()

    with pytest.raises(ValueError):
        await task_service.list_tasks(
            session,
            uuid.uuid4(),
            status="archived",
        )


async def test_list_tasks_returns_service_results():
    tasks = [_task(uuid.uuid4(), "One"), _task(uuid.uuid4(), "Two")]
    session = _TaskSession(tasks=tasks)

    result = await task_service.list_tasks(session, uuid.uuid4())

    assert result == tasks


async def test_resolve_task_by_full_uuid():
    task = _task(uuid.uuid4(), "Pay the bill")
    session = _TaskSession(scalar_task=task)

    resolved = await task_service.resolve_task(
        session,
        task.user_id,
        str(task.id),
    )

    assert resolved is task


async def test_resolve_task_full_uuid_not_found():
    session = _TaskSession(scalar_task=None)

    with pytest.raises(ValueError, match="not found"):
        await task_service.resolve_task(
            session,
            uuid.uuid4(),
            str(uuid.uuid4()),
        )


async def test_resolve_task_rejects_already_done():
    task = _task(uuid.uuid4(), "Old task", status="done")
    session = _TaskSession(scalar_task=task)

    with pytest.raises(ValueError, match="already done"):
        await task_service.resolve_task(
            session,
            task.user_id,
            str(task.id),
        )


async def test_resolve_task_by_unique_short_prefix():
    wanted = _task(
        uuid.UUID("abcd1234-0000-4000-8000-000000000000"),
        "Wanted",
    )
    other = _task(
        uuid.UUID("ffff9999-0000-4000-8000-000000000000"),
        "Other",
    )
    session = _TaskSession(tasks=[other, wanted])

    resolved = await task_service.resolve_task(
        session,
        wanted.user_id,
        "abcd1234",
    )

    assert resolved is wanted


async def test_resolve_task_ambiguous_short_prefix():
    first = _task(
        uuid.UUID("abcd1234-1111-4000-8000-000000000000"),
        "First",
    )
    second = _task(
        uuid.UUID("abcd1234-2222-4000-8000-000000000000"),
        "Second",
    )
    session = _TaskSession(tasks=[first, second])

    with pytest.raises(ValueError, match="more than one"):
        await task_service.resolve_task(
            session,
            first.user_id,
            "abcd1234",
        )


async def test_resolve_task_unknown_short_prefix():
    task = _task(
        uuid.UUID("abcd1234-0000-4000-8000-000000000000"),
        "Only task",
    )
    session = _TaskSession(tasks=[task])

    with pytest.raises(ValueError, match="not found"):
        await task_service.resolve_task(
            session,
            task.user_id,
            "deadbeef",
        )


async def test_resolve_task_rejects_tiny_prefix():
    session = _TaskSession()

    with pytest.raises(ValueError, match="valid task_id"):
        await task_service.resolve_task(session, uuid.uuid4(), "abc")


async def test_complete_task_marks_done():
    task = _task(uuid.uuid4(), "Finish the report")
    session = _TaskSession(scalar_task=task)

    result = await task_service.complete_task(
        session,
        task.user_id,
        str(task.id),
    )

    assert result is task
    assert task.status == "done"
    assert task.completed_at is not None
    assert task.completed_at.tzinfo is not None
    assert session.committed is True


async def test_complete_task_by_short_id():
    task = _task(
        uuid.UUID("cafe0001-0000-4000-8000-000000000000"),
        "Call the bank",
    )
    session = _TaskSession(tasks=[task])

    result = await task_service.complete_task(
        session,
        task.user_id,
        "cafe0001",
    )

    assert result is task
    assert task.status == "done"


async def test_delete_task_removes_it():
    task = _task(uuid.uuid4(), "Obsolete")
    session = _TaskSession(scalar_task=task)

    result = await task_service.delete_task(
        session,
        task.user_id,
        task.id,
    )

    assert result is task
    assert session.deleted == [task]
    assert session.committed is True


# ---------------------------------------------------------------------------
# Task tool declarations + dispatch
# ---------------------------------------------------------------------------


def test_task_tools_are_declared():
    names = {tool["name"] for tool in ai_tools.TOOL_DECLARATIONS}

    assert {
        "create_task",
        "list_tasks",
        "complete_task",
        "delete_task",
    } <= names


def test_task_mutators_are_in_duplicate_guard():
    assert {
        "create_task",
        "complete_task",
        "delete_task",
    } <= ai_service.MUTATING_TOOLS


class _DispatchSession:
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


def _dispatch_factory():
    return _DispatchSession()


def _patch_actor(monkeypatch, user_id):
    async def fake_resolve(session, telegram_user_id):
        assert telegram_user_id == 42
        return SimpleNamespace(id=user_id)

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )


async def test_create_task_dispatch(monkeypatch):
    user_id = uuid.uuid4()
    recorded = {}

    async def fake_create(session, uid, *, title, notes=None, due_at=None):
        recorded.update(
            user_id=uid,
            title=title,
            notes=notes,
            due_at=due_at,
        )
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=title,
            notes=notes,
            status="open",
            due_at=due_at,
            created_at=None,
            completed_at=None,
        )

    _patch_actor(monkeypatch, user_id)
    monkeypatch.setattr(task_service, "create_task", fake_create)

    result = await ai_tools.execute_ai_tool(
        _dispatch_factory,
        telegram_user_id=42,
        tool_name="create_task",
        arguments={
            "title": "Renew the passport",
            "notes": "Needs a photo",
        },
    )

    assert result["success"] is True
    assert recorded["user_id"] == user_id
    assert recorded["title"] == "Renew the passport"
    assert recorded["notes"] == "Needs a photo"
    assert result["task"]["title"] == "Renew the passport"
    assert result["task"]["status"] == "open"
    assert result["task"]["short_id"] == result["task"]["id"][:8]


async def test_complete_and_delete_task_dispatch(monkeypatch):
    user_id = uuid.uuid4()
    calls = []

    def _fake_task():
        return SimpleNamespace(
            id=uuid.uuid4(),
            title="Water the plants",
            notes=None,
            status="done",
            due_at=None,
            created_at=None,
            completed_at=datetime.now(timezone.utc),
        )

    async def fake_complete(session, uid, task_ref):
        calls.append(("complete", uid, task_ref))
        return _fake_task()

    async def fake_delete(session, uid, task_ref):
        calls.append(("delete", uid, task_ref))
        return _fake_task()

    _patch_actor(monkeypatch, user_id)
    monkeypatch.setattr(task_service, "complete_task", fake_complete)
    monkeypatch.setattr(task_service, "delete_task", fake_delete)

    result = await ai_tools.execute_ai_tool(
        _dispatch_factory,
        telegram_user_id=42,
        tool_name="complete_task",
        arguments={"task_id": "abcd1234"},
    )
    assert result["success"] is True
    assert result["message"] == "Task marked as done."

    result = await ai_tools.execute_ai_tool(
        _dispatch_factory,
        telegram_user_id=42,
        tool_name="delete_task",
        arguments={"task_id": "abcd1234"},
    )
    assert result["success"] is True

    assert calls == [
        ("complete", user_id, "abcd1234"),
        ("delete", user_id, "abcd1234"),
    ]


async def test_create_task_dispatch_rejects_empty_title(monkeypatch):
    _patch_actor(monkeypatch, uuid.uuid4())

    result = await ai_tools.execute_ai_tool(
        _dispatch_factory,
        telegram_user_id=42,
        tool_name="create_task",
        arguments={"title": "  "},
    )

    assert result["success"] is False
    assert "title" in result["error"]


async def test_task_tools_reject_inactive_user(monkeypatch):
    async def fake_resolve(session, telegram_user_id):
        return None

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )

    result = await ai_tools.execute_ai_tool(
        _dispatch_factory,
        telegram_user_id=999,
        tool_name="list_tasks",
        arguments={},
    )

    assert result["success"] is False
    assert "not active" in result["error"]


# ---------------------------------------------------------------------------
# Compression: pure helpers
# ---------------------------------------------------------------------------


def test_should_compress_threshold():
    assert conversation_service.should_compress(40, 0) is False
    assert conversation_service.should_compress(41, 0) is True
    assert conversation_service.should_compress(60, 20) is False
    assert conversation_service.should_compress(61, 20) is True
    assert conversation_service.should_compress(10, 0) is False


def test_build_summary_prompt_without_previous():
    chunk = [
        {"role": "user", "text": "I live in Dhaka."},
        {"role": "model", "text": "Noted!"},
        {"role": "user", "text": "My cat is Miso."},
    ]

    prompt = conversation_service.build_summary_prompt(None, chunk)

    assert "preserving facts, preferences, decisions" in prompt
    assert "User: I live in Dhaka." in prompt
    assert "Assistant: Noted!" in prompt
    assert "User: My cat is Miso." in prompt
    assert "Summary of the earlier part" not in prompt


def test_build_summary_prompt_merges_previous():
    chunk = [{"role": "user", "text": "I moved to Sylhet."}]

    prompt = conversation_service.build_summary_prompt(
        "User lives in Dhaka and has a cat named Miso.",
        chunk,
    )

    assert "User lives in Dhaka and has a cat named Miso." in prompt
    assert "fold into that summary" in prompt
    assert "User: I moved to Sylhet." in prompt


def test_cap_summary():
    assert conversation_service.cap_summary("short") == "short"

    long_text = "word " * 2000
    capped = conversation_service.cap_summary(long_text)

    assert len(capped) <= conversation_service.SUMMARY_MAX_CHARACTERS + 1
    assert capped.endswith("…")


async def test_get_history_with_summary_returns_recent_unsummarized():
    rows = [
        SimpleNamespace(
            role="user" if index % 2 == 0 else "model",
            content=f"message {index}",
        )
        for index in range(25)
    ]

    class _Result:
        def all(self):
            return rows

    class _Session:
        async def execute(self, *args, **kwargs):
            return _Result()

    conversation = SimpleNamespace(
        id=uuid.uuid4(),
        summary="EARLIER SUMMARY",
        summary_message_count=0,
    )

    summary, history = (
        await conversation_service.get_history_with_summary(
            _Session(),
            conversation,
        )
    )

    assert summary == "EARLIER SUMMARY"
    assert len(history) == conversation_service.SUMMARY_RECENT_MESSAGES
    assert history[0] == {"role": "model", "text": "message 5"}
    assert history[-1] == {"role": "user", "text": "message 24"}


# ---------------------------------------------------------------------------
# Compression: the compression pass with a mocked Gemini client
# ---------------------------------------------------------------------------


def _text_response(text):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=text)],
                ),
                finish_reason="STOP",
            )
        ]
    )


class _SummaryModels:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    async def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        if self._error is not None:
            raise self._error
        return self._response


class _SummarySession:
    def __init__(self, conversation):
        self._conversation = conversation

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, model, pk):
        return self._conversation


def _patch_compression(
    monkeypatch,
    *,
    total,
    chunk_size=20,
    saved=None,
):
    async def fake_count(session, conversation_id):
        return total

    async def fake_chunk(session, conversation, limit=None):
        return [
            {"role": "user", "text": f"old message {index}"}
            for index in range(chunk_size)
        ]

    async def fake_save(session, conversation, summary_text, folded):
        if saved is not None:
            saved.update(text=summary_text, folded=folded)

    monkeypatch.setattr(
        conversation_service,
        "count_conversation_messages",
        fake_count,
    )
    monkeypatch.setattr(
        conversation_service,
        "get_oldest_unsummarized_messages",
        fake_chunk,
    )
    monkeypatch.setattr(
        conversation_service,
        "save_conversation_summary",
        fake_save,
    )


async def test_maybe_compress_folds_oldest_chunk(monkeypatch):
    conversation = SimpleNamespace(
        id=uuid.uuid4(),
        summary="OLD SUMMARY",
        summary_message_count=20,
    )
    saved = {}
    _patch_compression(monkeypatch, total=65, saved=saved)

    models = _SummaryModels(response=_text_response("MERGED SUMMARY"))
    client = SimpleNamespace(models=models)

    result = await ai_service._maybe_compress_conversation(
        client,
        lambda: _SummarySession(conversation),
        conversation.id,
    )

    assert result == "MERGED SUMMARY"
    assert saved == {"text": "MERGED SUMMARY", "folded": 20}

    # The summarization call is a plain text call: no tools, and
    # the prompt carries the previous summary plus the chunk.
    _, contents, config = models.calls[0]
    assert not config.tools
    assert "OLD SUMMARY" in contents
    assert "old message 0" in contents


async def test_maybe_compress_skips_when_below_threshold(monkeypatch):
    conversation = SimpleNamespace(
        id=uuid.uuid4(),
        summary=None,
        summary_message_count=0,
    )
    saved = {}
    _patch_compression(monkeypatch, total=30, saved=saved)

    models = _SummaryModels(response=_text_response("UNUSED"))
    client = SimpleNamespace(models=models)

    result = await ai_service._maybe_compress_conversation(
        client,
        lambda: _SummarySession(conversation),
        conversation.id,
    )

    assert result is None
    assert models.calls == []
    assert saved == {}


async def test_maybe_compress_failure_is_swallowed(monkeypatch):
    conversation = SimpleNamespace(
        id=uuid.uuid4(),
        summary=None,
        summary_message_count=0,
    )
    saved = {}
    _patch_compression(monkeypatch, total=100, saved=saved)

    models = _SummaryModels(error=RuntimeError("provider down"))
    client = SimpleNamespace(models=models)

    result = await ai_service._maybe_compress_conversation(
        client,
        lambda: _SummarySession(conversation),
        conversation.id,
    )

    assert result is None
    assert saved == {}


# ---------------------------------------------------------------------------
# Compression: full generate flow
# ---------------------------------------------------------------------------


class _CapturingModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate_content(self, model, contents, config):
        snapshot = [
            (content.role, [part.text for part in (content.parts or []) if part.text])
            for content in contents
        ]
        self.calls.append((snapshot, config))
        return self._responses.pop(0)


class _FakeAioContext:
    def __init__(self, models):
        self.models = models

    async def __aenter__(self):
        return SimpleNamespace(models=self.models)

    async def __aexit__(self, *exc):
        return False


class _FakeGenaiClient:
    models = None

    def __init__(self, api_key=None):
        self.aio = _FakeAioContext(type(self).models)


async def test_generate_flow_uses_summary_and_excludes_summarized(monkeypatch):
    models = _CapturingModels([_text_response("Here is my answer.")])
    _FakeGenaiClient.models = models

    monkeypatch.setattr(ai_service.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_service.genai, "Client", _FakeGenaiClient)

    async def fake_reserve(session_factory, telegram_user_id):
        return uuid.uuid4(), "user", "Test User", "Asia/Dhaka"

    async def fake_none(session_factory, telegram_user_id):
        return None

    async def fake_finish(*args, **kwargs):
        return None

    recent_history = [
        {"role": "user", "text": "recent question"},
        {"role": "model", "text": "recent answer"},
    ]

    async def fake_load_summary(session_factory, conversation_id):
        return "EARLIER SUMMARY", recent_history

    async def fake_compress(client, session_factory, conversation_id):
        return None

    monkeypatch.setattr(ai_service, "_reserve_request", fake_reserve)
    monkeypatch.setattr(ai_service, "_load_memories_text", fake_none)
    monkeypatch.setattr(ai_service, "_load_user_language", fake_none)
    monkeypatch.setattr(ai_service, "_finish_usage", fake_finish)
    monkeypatch.setattr(
        ai_service,
        "_load_conversation_summary",
        fake_load_summary,
    )
    monkeypatch.setattr(
        ai_service,
        "_maybe_compress_conversation",
        fake_compress,
    )

    reply = await ai_service.generate_ai_reply(
        None,
        telegram_user_id=123,
        prompt="What did we decide?",
        history=[
            {"role": "user", "text": "SUMMARIZED OLD MESSAGE"},
            {"role": "model", "text": "SUMMARIZED OLD ANSWER"},
        ],
        conversation_id=uuid.uuid4(),
    )

    assert reply.text == "Here is my answer."
    assert len(models.calls) == 1

    snapshot, config = models.calls[0]

    # The summary is in the system instruction.
    assert "Summary of earlier in this conversation:" in (
        config.system_instruction
    )
    assert "EARLIER SUMMARY" in config.system_instruction

    # The contents carry the recent unsummarized history plus the
    # new prompt — and none of the summarized messages the caller
    # originally passed in.
    all_texts = [
        text for _, parts in snapshot for text in parts
    ]
    assert "recent question" in all_texts
    assert "recent answer" in all_texts
    assert "What did we decide?" in all_texts
    assert "SUMMARIZED OLD MESSAGE" not in all_texts
    assert "SUMMARIZED OLD ANSWER" not in all_texts
