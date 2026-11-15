"""Regression test for the Gemini tool-calling round trip.

Guards the bug where Part.from_function_response(..., id=...) raised
TypeError on google-genai 1.x after the tool had already executed,
which turned every admin tool request into an AIProviderError.
"""

from google.genai import types

from app.services import ai_service


def _function_call_response():
    part = types.Part.from_function_call(
        name="create_invite",
        args={"max_uses": 1, "expires_in_days": 7},
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
        self.calls = 0

    async def generate_content(self, model, contents, config):
        self.calls += 1
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


async def test_tool_round_trip_returns_text(monkeypatch):
    executed = []

    async def fake_execute(session_factory, *, telegram_user_id, tool_name, arguments):
        executed.append((tool_name, arguments))
        return {"success": True, "code": "TESTCODE", "max_uses": 1}

    monkeypatch.setattr(ai_service, "execute_ai_tool", fake_execute)

    client = _FakeClient([_function_call_response(), _text_response("Invitation created.")])
    response, tool_used = await ai_service._generate_with_tools(
        client,
        model="test-model",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Create an invitation for one user, valid for 7 days.")],
            )
        ],
        system_instruction="test",
        session_factory=None,
        telegram_user_id=123,
    )

    assert tool_used is True
    assert executed == [("create_invite", {"max_uses": 1, "expires_in_days": 7})]
    assert ai_service._get_response_text(response) == "Invitation created."


async def test_plain_reply_uses_no_tools():
    client = _FakeClient([_text_response("Hello!")])
    response, tool_used = await ai_service._generate_with_tools(
        client,
        model="test-model",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="hello")],
            )
        ],
        system_instruction="test",
        session_factory=None,
        telegram_user_id=123,
    )

    assert tool_used is False
    assert ai_service._get_response_text(response) == "Hello!"
