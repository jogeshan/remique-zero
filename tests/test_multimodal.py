"""Phase 8 (multimodal) tests: attachments through the AI service,
the document allowlist, and the photo handler wiring."""

import uuid
from types import SimpleNamespace

from google.genai import types

from app.handlers import media
from app.services import ai_service
from app.services.ai_service import AIReply


# ---------------------------------------------------------------------------
# _build_contents: attachments land on the current user turn only
# ---------------------------------------------------------------------------


def _parts_of(content):
    return list(content.parts or [])


def test_build_contents_with_attachments():
    contents = ai_service._build_contents(
        [{"role": "user", "text": "earlier"}],
        "What is in this picture?",
        attachments=[("image/png", b"PNGDATA")],
    )

    # History turn stays text-only.
    history_parts = _parts_of(contents[0])
    assert len(history_parts) == 1
    assert history_parts[0].text == "earlier"

    user_parts = _parts_of(contents[-1])
    assert len(user_parts) == 2
    assert user_parts[0].inline_data.data == b"PNGDATA"
    assert user_parts[0].inline_data.mime_type == "image/png"
    assert user_parts[1].text == "What is in this picture?"


def test_build_contents_without_attachments_unchanged():
    contents = ai_service._build_contents([], "hello")

    assert len(contents) == 1
    parts = _parts_of(contents[0])
    assert len(parts) == 1
    assert parts[0].text == "hello"


# ---------------------------------------------------------------------------
# generate_ai_reply: attachments reach Gemini and survive the tool loop
# ---------------------------------------------------------------------------


def _function_call_response():
    part = types.Part.from_function_call(
        name="create_invite",
        args={"max_uses": 1},
    )
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[part]),
                finish_reason="STOP",
            )
        ]
    )


def _text_response(text="It is a cat."):
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


class _CapturingModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate_content(self, model, contents, config):
        snapshot = [
            (content.role, list(content.parts or []))
            for content in contents
        ]
        self.calls.append(snapshot)
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


async def test_generate_ai_reply_with_attachments_and_tool_round(monkeypatch):
    models = _CapturingModels(
        [_function_call_response(), _text_response("Done, and it is a cat.")]
    )
    _FakeGenaiClient.models = models

    monkeypatch.setattr(ai_service.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_service.genai, "Client", _FakeGenaiClient)

    async def fake_reserve(session_factory, telegram_user_id):
        return uuid.uuid4(), "user", "Test User", "Asia/Dhaka"

    async def fake_memories(session_factory, telegram_user_id):
        return None

    async def fake_finish(*args, **kwargs):
        return None

    async def fake_execute(session_factory, *, telegram_user_id, tool_name, arguments):
        return {"success": True, "code": "TESTCODE"}

    monkeypatch.setattr(ai_service, "_reserve_request", fake_reserve)
    monkeypatch.setattr(ai_service, "_load_memories_text", fake_memories)
    monkeypatch.setattr(ai_service, "_finish_usage", fake_finish)
    monkeypatch.setattr(ai_service, "execute_ai_tool", fake_execute)

    reply = await ai_service.generate_ai_reply(
        None,
        telegram_user_id=123,
        prompt="What is in this picture?",
        history=[],
        attachments=[("image/png", b"PNGDATA")],
    )

    assert reply.text == "Done, and it is a cat."
    assert len(models.calls) == 2

    for snapshot in models.calls:
        user_turns = [parts for role, parts in snapshot if role == "user"]
        first_turn = user_turns[0]
        assert any(
            part.inline_data is not None
            and part.inline_data.data == b"PNGDATA"
            and part.inline_data.mime_type == "image/png"
            for part in first_turn
        ), "attachment bytes part must be present on every tool round"


# ---------------------------------------------------------------------------
# Document allowlist
# ---------------------------------------------------------------------------


def test_is_supported_document_by_mime():
    assert media.is_supported_document("application/pdf", "x.bin")
    assert media.is_supported_document("text/plain", None)
    assert media.is_supported_document("text/markdown", None)
    assert media.is_supported_document("text/csv", None)


def test_is_supported_document_by_extension_when_mime_missing():
    assert media.is_supported_document(None, "notes.md")
    assert media.is_supported_document("", "report.PDF")
    assert media.is_supported_document("application/octet-stream", "data.csv")


def test_is_supported_document_rejects_others():
    assert not media.is_supported_document("image/png", "photo.png")
    assert not media.is_supported_document("application/zip", "a.zip")
    assert not media.is_supported_document(None, None)
    assert not media.is_supported_document(None, "noextension")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


class _FakeTelegramFile:
    def __init__(self, data):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class _FakeBot:
    def __init__(self, data=b"jpegdata"):
        self._data = data
        self.requested_file_ids = []

    async def send_chat_action(self, chat_id, action):
        return None

    async def get_file(self, file_id):
        self.requested_file_ids.append(file_id)
        return _FakeTelegramFile(self._data)


class _FakeMessage:
    def __init__(self, **kwargs):
        self.text = None
        self.caption = kwargs.get("caption")
        self.photo = kwargs.get("photo")
        self.voice = kwargs.get("voice")
        self.audio = kwargs.get("audio")
        self.document = kwargs.get("document")
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def _fake_update(message):
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=7, type="private"),
    )


def _patch_handler_dependencies(monkeypatch, captured):
    monkeypatch.setattr(media, "get_session_factory", lambda: None)

    async def fake_context(session_factory, telegram_user_id):
        return uuid.uuid4(), []

    async def fake_generate(session_factory, **kwargs):
        captured["generate"] = kwargs
        return AIReply(
            text="It is a cat sitting on a mat.",
            model="test-model",
            input_tokens=3,
            output_tokens=5,
            latency_ms=1,
        )

    async def fake_save(session_factory, **kwargs):
        captured["save"] = kwargs

    async def fake_record(session_factory, telegram_user_id, **kwargs):
        captured["asset"] = kwargs

    monkeypatch.setattr(media, "get_active_conversation_context", fake_context)
    monkeypatch.setattr(media, "generate_ai_reply", fake_generate)
    monkeypatch.setattr(media, "save_conversation_exchange", fake_save)
    monkeypatch.setattr(media, "_record_media_asset", fake_record)


async def test_photo_handler_sends_largest_photo_with_caption(monkeypatch):
    captured = {}
    _patch_handler_dependencies(monkeypatch, captured)

    message = _FakeMessage(
        caption="What is this?",
        photo=[
            SimpleNamespace(file_id="small-id", file_size=100),
            SimpleNamespace(file_id="large-id", file_size=999),
        ],
    )
    bot = _FakeBot()
    context = SimpleNamespace(bot=bot)

    await media.photo_message_handler(_fake_update(message), context)

    assert bot.requested_file_ids == ["large-id"]
    assert captured["generate"]["prompt"] == "What is this?"
    assert captured["generate"]["attachments"] == [
        ("image/jpeg", b"jpegdata")
    ]
    assert captured["save"]["prompt"] == "What is this?"
    assert captured["asset"]["media_type"] == "photo"
    assert captured["asset"]["storage_key"] == "large-id"
    assert any(
        "cat sitting on a mat" in reply for reply in message.replies
    )


async def test_photo_handler_without_caption_uses_defaults(monkeypatch):
    captured = {}
    _patch_handler_dependencies(monkeypatch, captured)

    message = _FakeMessage(
        photo=[SimpleNamespace(file_id="only-id", file_size=10)],
    )
    context = SimpleNamespace(bot=_FakeBot())

    await media.photo_message_handler(_fake_update(message), context)

    assert captured["generate"]["prompt"] == media.PHOTO_DEFAULT_PROMPT
    assert captured["save"]["prompt"] == "[Photo]"


async def test_photo_handler_rejects_oversized_file(monkeypatch):
    captured = {}
    _patch_handler_dependencies(monkeypatch, captured)

    message = _FakeMessage(
        photo=[
            SimpleNamespace(
                file_id="huge-id",
                file_size=media.MAX_MEDIA_BYTES + 1,
            )
        ],
    )
    bot = _FakeBot()
    context = SimpleNamespace(bot=bot)

    await media.photo_message_handler(_fake_update(message), context)

    assert bot.requested_file_ids == []
    assert "generate" not in captured
    assert any("too large" in reply for reply in message.replies)


async def test_document_handler_rejects_unsupported_type(monkeypatch):
    captured = {}
    _patch_handler_dependencies(monkeypatch, captured)

    message = _FakeMessage(
        document=SimpleNamespace(
            file_id="doc-id",
            file_size=10,
            file_name="photo.png",
            mime_type="image/png",
        ),
    )
    bot = _FakeBot()
    context = SimpleNamespace(bot=bot)

    await media.document_message_handler(_fake_update(message), context)

    assert bot.requested_file_ids == []
    assert "generate" not in captured
    assert any("PDF" in reply for reply in message.replies)
