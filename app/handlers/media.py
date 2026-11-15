import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.config import settings
from app.database import get_session_factory
from app.models import MediaAsset

from app.services import memory_service

from app.services.ai_service import (
    AIAccountInactive,
    AIConfigurationError,
    AIProviderError,
    AIQuotaExceeded,
    AIReply,
    AIServiceError,
    generate_ai_reply,
)

from app.services.conversation_service import (
    ConversationAccessError,
    get_active_conversation_context,
    save_conversation_exchange,
)

from app.handlers.ai import _send_long_message


logger = logging.getLogger(__name__)

MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_INPUT_CHARACTERS = 8000

SUPPORTED_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
}

DOCUMENT_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
}

PHOTO_DEFAULT_PROMPT = (
    "Describe this image. Answer any question visible in it, "
    "and be helpful about what it shows."
)

VOICE_PROMPT = (
    "The user sent a voice message. Transcribe what they said "
    "(they may speak Bangla, English, or Banglish), then respond "
    "to it helpfully in the same language they used."
)

DOCUMENT_DEFAULT_PROMPT = (
    "Read this document and summarize it. Answer any question "
    "in my caption if I gave one."
)


def is_supported_document(
    mime_type: str | None,
    filename: str | None,
) -> bool:
    """Allowlist check for document messages (mime, else extension)."""
    if mime_type and mime_type.strip().lower() in SUPPORTED_DOCUMENT_MIME_TYPES:
        return True

    if filename:
        name = filename.strip().lower()
        return any(
            name.endswith(extension)
            for extension in DOCUMENT_MIME_BY_EXTENSION
        )

    return False


def _document_mime_type(
    mime_type: str | None,
    filename: str | None,
) -> str:
    """Pick the mime type to send to Gemini for a supported document."""
    if mime_type and mime_type.strip().lower() in SUPPORTED_DOCUMENT_MIME_TYPES:
        return mime_type.strip().lower()

    if filename:
        name = filename.strip().lower()
        for extension, mapped in DOCUMENT_MIME_BY_EXTENSION.items():
            if name.endswith(extension):
                return mapped

    return "application/pdf"


async def _record_media_asset(
    session_factory,
    telegram_user_id: int,
    *,
    storage_key: str,
    media_type: str,
    file_size_bytes: int | None,
) -> None:
    """
    Insert a MediaAsset row for a downloaded file.

    Bookkeeping only: any failure is logged and swallowed so it can
    never break the user's reply.
    """
    try:
        async with session_factory() as session:
            user = await memory_service.get_active_user_by_telegram_id(
                session,
                telegram_user_id,
            )

            if user is None:
                return

            session.add(
                MediaAsset(
                    user_id=user.id,
                    storage_key=storage_key,
                    media_type=media_type,
                    file_size_bytes=file_size_bytes,
                    processing_status="processed",
                )
            )

            await session.commit()

    except Exception:
        logger.exception(
            "Unable to record media asset for Telegram user %s",
            telegram_user_id,
        )


async def _handle_media_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    file_size: int | None,
    media_type: str,
    mime_type: str,
    prompt: str,
    stored_prompt: str,
) -> None:
    """
    Shared flow for photo, voice/audio, and document messages.

    Mirrors ai_message_handler: same private-chat rule, typing
    indicator, conversation loading, persistence, and user-facing
    error messages.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "Please chat with Remique Zero privately to use AI."
        )
        return

    if file_size is not None and file_size > MAX_MEDIA_BYTES:
        await message.reply_text(
            "That file is too large for me to read. "
            "Please send a file smaller than 10 MB."
        )
        return

    if len(prompt) > MAX_INPUT_CHARACTERS:
        await message.reply_text(
            "Your message is too long. "
            "Please send a shorter message."
        )
        return

    try:
        await context.bot.send_chat_action(
            chat_id=chat.id,
            action=ChatAction.TYPING,
        )
    except Exception:
        logger.debug("Unable to send Telegram typing indicator.")

    session_factory = get_session_factory()

    try:
        conversation_id, history = (
            await get_active_conversation_context(
                session_factory,
                user.id,
            )
        )

        telegram_file = await context.bot.get_file(file_id)
        data = bytes(await telegram_file.download_as_bytearray())

        await _record_media_asset(
            session_factory,
            user.id,
            storage_key=file_id,
            media_type=media_type,
            file_size_bytes=file_size if file_size is not None else len(data),
        )

        result: AIReply = await generate_ai_reply(
            session_factory,
            telegram_user_id=user.id,
            prompt=prompt,
            history=history,
            attachments=[(mime_type, data)],
            conversation_id=conversation_id,
        )

        try:
            await save_conversation_exchange(
                session_factory,
                telegram_user_id=user.id,
                conversation_id=conversation_id,
                prompt=stored_prompt,
                response=result.text,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        except Exception:
            logger.exception(
                "AI response generated but conversation persistence failed "
                "for Telegram user %s",
                user.id,
            )

            await _send_long_message(message, result.text)

            await message.reply_text(
                "⚠️ Your response was generated, but I couldn't save "
                "this exchange to conversation history."
            )
            return

        await _send_long_message(message, result.text)

    except (AIAccountInactive, ConversationAccessError):
        await message.reply_text(
            "Your Remique Zero account is not active. "
            "Please contact the administrator."
        )

    except AIQuotaExceeded:
        await message.reply_text(
            f"You've reached your daily AI request limit of "
            f"{settings.max_ai_requests_per_user_per_day}.\n\n"
            "Your allowance resets at midnight in your account timezone."
        )

    except AIConfigurationError:
        logger.error("Gemini API configuration is incomplete.")

        await message.reply_text(
            "The AI service is not configured correctly. "
            "Please contact the administrator."
        )

    except AIProviderError:
        await message.reply_text(
            "I'm temporarily unable to process your message. "
            "Please try again later."
        )

    except AIServiceError as exc:
        await message.reply_text(str(exc))

    except Exception:
        logger.exception(
            "Unexpected media handler error for Telegram user %s",
            user.id,
        )

        await message.reply_text(
            "Something went wrong while processing your message."
        )


async def photo_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message or not message.photo:
        return

    # Telegram sends several sizes; the last one is the largest.
    photo = message.photo[-1]
    caption = (message.caption or "").strip()

    await _handle_media_message(
        update,
        context,
        file_id=photo.file_id,
        file_size=photo.file_size,
        media_type="photo",
        mime_type="image/jpeg",
        prompt=caption or PHOTO_DEFAULT_PROMPT,
        stored_prompt=caption or "[Photo]",
    )


async def voice_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message:
        return

    voice = message.voice
    audio = message.audio

    if voice is not None:
        file_id = voice.file_id
        file_size = voice.file_size
        media_type = "voice"
        mime_type = "audio/ogg"
    elif audio is not None:
        file_id = audio.file_id
        file_size = audio.file_size
        media_type = "audio"
        mime_type = audio.mime_type or "audio/mpeg"
    else:
        return

    caption = (message.caption or "").strip()

    await _handle_media_message(
        update,
        context,
        file_id=file_id,
        file_size=file_size,
        media_type=media_type,
        mime_type=mime_type,
        prompt=VOICE_PROMPT,
        stored_prompt=caption or "[Voice message]",
    )


async def document_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message or not message.document:
        return

    document = message.document
    filename = document.file_name

    if not is_supported_document(document.mime_type, filename):
        await message.reply_text(
            "I can't read that type of document yet. "
            "I can read PDF and text documents: PDF, TXT, MD, and CSV files."
        )
        return

    caption = (message.caption or "").strip()

    if caption:
        stored_prompt = f"[Document: {filename}] {caption}"
    else:
        stored_prompt = f"[Document: {filename}]"

    await _handle_media_message(
        update,
        context,
        file_id=document.file_id,
        file_size=document.file_size,
        media_type="document",
        mime_type=_document_mime_type(document.mime_type, filename),
        prompt=caption or DOCUMENT_DEFAULT_PROMPT,
        stored_prompt=stored_prompt,
    )
