"""Tests for the AI tool-framework hardening.

Covers three protections:
- identical mutating tool calls within one AI request execute once;
- every AI tool execution is recorded via the audit-log helper;
- every failure result carries both "success": False and an "error".
"""

import uuid
from unittest.mock import AsyncMock

from google.genai import types

from app.services import ai_service, ai_tools


# ---------------------------------------------------------------------------
# Fakes for the Gemini tool loop (modelled on tests/test_ai_tool_flow.py)
# ---------------------------------------------------------------------------


def _function_call_response(name="create_invite", args=None):
    part = types.Part.from_function_call(
        name=name,
        args=args or {"max_uses": 1, "expires_in_days": 7},
    )
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[part]),
                finish_reason="STOP",
            )
        ]
    )


def _text_response(text="Done."):
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


class _FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)

    async def generate_content(self, model, contents, config):
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


def _contents():
    return [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text="Create an invitation.")],
        )
    ]


# ---------------------------------------------------------------------------
# Duplicate guard
# ---------------------------------------------------------------------------


async def test_duplicate_mutating_call_executes_once(monkeypatch):
    executed = []

    async def fake_execute(session_factory, *, telegram_user_id, tool_name, arguments):
        executed.append(tool_name)
        return {"success": True, "code": "TESTCODE", "max_uses": 1}

    monkeypatch.setattr(ai_service, "execute_ai_tool", fake_execute)

    client = _FakeClient(
        [
            _function_call_response(),
            _function_call_response(),
            _text_response("Invitation created."),
        ]
    )
    response, tool_used = await ai_service._generate_with_tools(
        client,
        model="test-model",
        contents=_contents(),
        system_instruction="test",
        session_factory=None,
        telegram_user_id=123,
    )

    assert tool_used is True
    assert executed == ["create_invite"]
    assert ai_service._get_response_text(response) == "Invitation created."


async def test_duplicate_readonly_call_still_executes(monkeypatch):
    executed = []

    async def fake_execute(session_factory, *, telegram_user_id, tool_name, arguments):
        executed.append(tool_name)
        return {"success": True, "statistics": {}}

    monkeypatch.setattr(ai_service, "execute_ai_tool", fake_execute)

    client = _FakeClient(
        [
            _function_call_response("get_user_statistics", {}),
            _function_call_response("get_user_statistics", {}),
            _text_response("Here are the statistics."),
        ]
    )
    response, _ = await ai_service._generate_with_tools(
        client,
        model="test-model",
        contents=_contents(),
        system_instruction="test",
        session_factory=None,
        telegram_user_id=123,
    )

    assert executed == ["get_user_statistics", "get_user_statistics"]
    assert ai_service._get_response_text(response) == "Here are the statistics."


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class _DummySessionFactory:
    """Returns an async context manager yielding an AsyncMock session."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return AsyncMock()

    async def __aexit__(self, *exc):
        return False


async def test_execute_ai_tool_invokes_audit_helper(monkeypatch):
    calls = []

    async def recorder(session_factory, *, telegram_user_id, tool_name, result):
        calls.append((telegram_user_id, tool_name, result))

    monkeypatch.setattr(ai_tools, "_log_tool_execution", recorder)

    result = await ai_tools.execute_ai_tool(
        _DummySessionFactory(),
        telegram_user_id=123,
        tool_name="no_such_tool",
        arguments={},
    )

    assert result["success"] is False
    assert len(calls) == 1
    _, logged_tool, logged_result = calls[0]
    assert logged_tool == "no_such_tool"
    assert logged_result["success"] is False


class _AuditSession:
    def __init__(self, actor_id):
        self._actor_id = actor_id
        self.added = []
        self.committed = False

    async def scalar(self, statement):
        return self._actor_id

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _AuditFactory:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_audit_helper_writes_row_without_secrets():
    actor_id = uuid.uuid4()
    session = _AuditSession(actor_id)

    await ai_tools._log_tool_execution(
        _AuditFactory(session),
        telegram_user_id=123,
        tool_name="create_invite",
        result={"success": True, "code": "SECRET-CODE", "max_uses": 1},
    )

    assert session.committed is True
    assert len(session.added) == 1
    row = session.added[0]
    assert row.action == "ai_tool.create_invite"
    assert row.actor_user_id == actor_id
    assert row.metadata_json == {"success": True, "error": None}
    assert "SECRET-CODE" not in str(row.metadata_json)


async def test_audit_helper_failure_is_swallowed():
    def broken_factory():
        raise RuntimeError("no database")

    # Must not raise: audit failures never change the tool outcome.
    await ai_tools._log_tool_execution(
        broken_factory,
        telegram_user_id=123,
        tool_name="create_invite",
        result={"success": False, "error": "boom"},
    )


# ---------------------------------------------------------------------------
# Consistent error shape
# ---------------------------------------------------------------------------


async def _silence_audit(monkeypatch):
    async def noop(session_factory, *, telegram_user_id, tool_name, result):
        return None

    monkeypatch.setattr(ai_tools, "_log_tool_execution", noop)


async def test_unknown_tool_error_shape(monkeypatch):
    await _silence_audit(monkeypatch)

    result = await ai_tools.execute_ai_tool(
        _DummySessionFactory(),
        telegram_user_id=123,
        tool_name="no_such_tool",
        arguments={},
    )

    assert result["success"] is False
    assert result["error"]


async def test_validation_error_shape(monkeypatch):
    await _silence_audit(monkeypatch)

    result = await ai_tools.execute_ai_tool(
        _DummySessionFactory(),
        telegram_user_id=123,
        tool_name="create_invite",
        arguments={"max_uses": 0},
    )

    assert result["success"] is False
    assert result["error"]


async def test_revoke_not_found_error_shape(monkeypatch):
    await _silence_audit(monkeypatch)

    async def fake_revoke(session, *, telegram_user_id, code):
        return False

    monkeypatch.setattr(ai_tools, "revoke_admin_invite", fake_revoke)

    result = await ai_tools.execute_ai_tool(
        _DummySessionFactory(),
        telegram_user_id=123,
        tool_name="revoke_invite",
        arguments={"code": "SOMECODE"},
    )

    assert result["success"] is False
    assert result["error"]
    assert result["message"]


async def test_set_user_status_no_change_error_shape(monkeypatch):
    await _silence_audit(monkeypatch)

    async def fake_set_status(session, *, actor_telegram_id, target_telegram_id, new_status):
        return False

    monkeypatch.setattr(ai_tools, "set_user_status", fake_set_status)

    result = await ai_tools.execute_ai_tool(
        _DummySessionFactory(),
        telegram_user_id=123,
        tool_name="set_user_status",
        arguments={"target_telegram_id": 456, "status": "disabled"},
    )

    assert result["success"] is False
    assert result["error"]
    assert result["message"]
