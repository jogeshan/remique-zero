"""Tests for Phase 5 persistent per-user memory.

Covers the pure prompt formatter, the AI tool declarations and
dispatch for the memory tools, and the system-prompt memory section.
DB-backed service tests are skipped: aiosqlite is not available in
the test environment, and the integration suite covers real-DB
behaviour against PostgreSQL.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import ai_tools, memory_service
from app.services.ai_service import build_system_instruction
from app.services.memory_service import format_memories_for_prompt


def _memory(content, category="fact"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        content=content,
        category=category,
        created_at=datetime(2026, 11, 15, tzinfo=timezone.utc),
    )


# --- format_memories_for_prompt ------------------------------------


def test_format_memories_empty():
    assert format_memories_for_prompt([]) == ""


def test_format_memories_bullets():
    memories = [
        _memory("My cat is named Miso"),
        _memory("Prefers tea over coffee", category="preference"),
    ]

    text = format_memories_for_prompt(memories)

    assert text == (
        "- [fact] My cat is named Miso\n"
        "- [preference] Prefers tea over coffee"
    )


def test_format_memories_truncates_on_whole_bullets():
    memories = [_memory("x" * 90) for _ in range(50)]

    text = format_memories_for_prompt(memories, max_chars=200)

    assert len(text) <= 200
    # No partial bullet (other than a possible first-line ellipsis):
    # every line is a complete "- [fact] xxx" bullet.
    for line in text.splitlines():
        assert line == "- [fact] " + "x" * 90


def test_format_memories_single_oversized_bullet_is_truncated():
    text = format_memories_for_prompt(
        [_memory("y" * 500)],
        max_chars=100,
    )

    assert len(text) == 100
    assert text.endswith("…")


# --- tool declarations ----------------------------------------------


def test_memory_tools_are_declared():
    names = {tool["name"] for tool in ai_tools.TOOL_DECLARATIONS}

    assert {"remember_fact", "recall_memories", "forget_memory"} <= names


# --- execute_ai_tool dispatch ----------------------------------------


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _session_factory():
    return _FakeSession()


async def test_remember_fact_dispatches_to_memory_service(monkeypatch):
    user_id = uuid.uuid4()
    saved = {}

    async def fake_resolve(session, telegram_user_id):
        assert telegram_user_id == 42
        return SimpleNamespace(id=user_id)

    async def fake_save(session, uid, content, category="fact"):
        saved.update(user_id=uid, content=content, category=category)
        return _memory(content, category)

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )
    monkeypatch.setattr(memory_service, "save_memory", fake_save)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="remember_fact",
        arguments={"content": "My cat is named Miso", "category": "fact"},
    )

    assert result["success"] is True
    assert saved == {
        "user_id": user_id,
        "content": "My cat is named Miso",
        "category": "fact",
    }
    assert result["memory"]["content"] == "My cat is named Miso"
    uuid.UUID(result["memory"]["id"])  # id is a valid UUID string


async def test_recall_and_forget_dispatch(monkeypatch):
    user_id = uuid.uuid4()
    memory = _memory("Likes tea", category="preference")
    deleted = {}

    async def fake_resolve(session, telegram_user_id):
        return SimpleNamespace(id=user_id)

    async def fake_list(session, uid, limit=50):
        assert uid == user_id
        return [memory]

    async def fake_delete(session, uid, memory_id):
        deleted.update(user_id=uid, memory_id=memory_id)
        return memory_id == memory.id and uid == user_id

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )
    monkeypatch.setattr(memory_service, "list_memories", fake_list)
    monkeypatch.setattr(memory_service, "delete_memory", fake_delete)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="recall_memories",
        arguments={},
    )
    assert result["success"] is True
    assert result["count"] == 1
    assert result["memories"][0]["id"] == str(memory.id)

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=42,
        tool_name="forget_memory",
        arguments={"memory_id": str(memory.id)},
    )
    assert result["success"] is True
    assert deleted == {"user_id": user_id, "memory_id": memory.id}


async def test_memory_tools_reject_inactive_user(monkeypatch):
    async def fake_resolve(session, telegram_user_id):
        return None

    monkeypatch.setattr(
        memory_service,
        "get_active_user_by_telegram_id",
        fake_resolve,
    )

    result = await ai_tools.execute_ai_tool(
        _session_factory,
        telegram_user_id=999,
        tool_name="remember_fact",
        arguments={"content": "anything"},
    )

    assert result["success"] is False
    assert "not active" in result["error"]


# --- system prompt ----------------------------------------------------


def test_system_instruction_includes_memories_section():
    instruction = build_system_instruction(
        "user",
        memories_text="- [fact] My cat is named Miso",
    )

    assert "What you remember about this user:" in instruction
    assert "My cat is named Miso" in instruction
    assert "update or delete" in instruction


def test_system_instruction_omits_memories_section_when_none():
    instruction = build_system_instruction("user", memories_text=None)

    assert "What you remember about this user:" not in instruction
