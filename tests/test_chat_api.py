"""Tests for the /api/v1/chat endpoints (app/api/chat.py)."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import chat


class _ScalarSession:
    def __init__(self, scalar_value=None, scalars_rows=None):
        self._scalar_value = scalar_value
        self._scalars_rows = scalars_rows or []

    async def scalar(self, statement):
        return self._scalar_value

    async def scalars(self, statement):
        return list(self._scalars_rows)


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", status="active")


def _account():
    return SimpleNamespace(telegram_user_id=424242)


@pytest.fixture
def pipeline(monkeypatch):
    """Stub the AI pipeline in the chat module namespace."""
    calls = {}

    async def fake_context(session_factory, telegram_user_id):
        calls["context_for"] = telegram_user_id
        return uuid.uuid4(), []

    async def fake_generate(
        session_factory,
        *,
        telegram_user_id,
        prompt,
        history=None,
        conversation_id=None,
    ):
        calls["prompt"] = prompt
        calls["generate_for"] = telegram_user_id
        return SimpleNamespace(
            text="Hello from Remique",
            model="gemini-test",
            input_tokens=10,
            output_tokens=5,
        )

    async def fake_save(session_factory, **kwargs):
        calls["saved"] = kwargs

    monkeypatch.setattr(chat, "get_active_conversation_context", fake_context)
    monkeypatch.setattr(chat, "generate_ai_reply", fake_generate)
    monkeypatch.setattr(chat, "save_conversation_exchange", fake_save)
    monkeypatch.setattr(chat, "get_session_factory", lambda: object())
    return calls


@pytest.mark.asyncio
async def test_post_chat_returns_reply_and_persists(pipeline):
    session = _ScalarSession(scalar_value=_account())
    payload = chat.ChatRequest(message="hi there")

    result = await chat.post_chat(payload, user=_user(), session=session)

    assert result == {"reply": "Hello from Remique", "model": "gemini-test"}
    assert pipeline["context_for"] == 424242
    assert pipeline["generate_for"] == 424242
    assert pipeline["prompt"] == "hi there"
    assert pipeline["saved"]["prompt"] == "hi there"
    assert pipeline["saved"]["response"] == "Hello from Remique"


@pytest.mark.asyncio
async def test_post_chat_without_telegram_link_is_409(pipeline):
    session = _ScalarSession(scalar_value=None)
    payload = chat.ChatRequest(message="hi")

    with pytest.raises(HTTPException) as excinfo:
        await chat.post_chat(payload, user=_user(), session=session)

    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_post_chat_quota_maps_to_429(monkeypatch, pipeline):
    from app.services.ai_service import AIQuotaExceeded

    async def quota_generate(*args, **kwargs):
        raise AIQuotaExceeded("quota")

    monkeypatch.setattr(chat, "generate_ai_reply", quota_generate)
    session = _ScalarSession(scalar_value=_account())
    payload = chat.ChatRequest(message="hi")

    with pytest.raises(HTTPException) as excinfo:
        await chat.post_chat(payload, user=_user(), session=session)

    assert excinfo.value.status_code == 429


@pytest.mark.asyncio
async def test_history_empty_without_conversation():
    session = _ScalarSession(scalar_value=None)

    result = await chat.get_chat_history(
        limit=50,
        user=_user(),
        session=session,
    )

    assert result == {"messages": []}


@pytest.mark.asyncio
async def test_history_returns_chronological_messages():
    from datetime import datetime, timezone

    stamp = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    conversation = SimpleNamespace(id=uuid.uuid4())
    rows = [
        SimpleNamespace(role="assistant", content="second", created_at=stamp),
        SimpleNamespace(role="user", content="first", created_at=stamp),
    ]
    session = _ScalarSession(scalar_value=conversation, scalars_rows=rows)

    result = await chat.get_chat_history(
        limit=50,
        user=_user(),
        session=session,
    )

    contents = [m["content"] for m in result["messages"]]
    assert contents == ["first", "second"]


def test_chat_routes_registered_and_require_auth():
    from fastapi.testclient import TestClient

    from app.database import get_db_session
    from app.main import app

    async def fake_session():
        yield object()

    app.dependency_overrides[get_db_session] = fake_session
    try:
        client = TestClient(app, raise_server_exceptions=False)

        history = client.get("/api/v1/chat/history")
        assert history.status_code == 401

        post = client.post("/api/v1/chat", json={"message": "hi"})
        assert post.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db_session, None)
