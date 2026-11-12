import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.config import settings
from app.database import get_session_factory

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
    start_new_conversation,
)


logger = logging.getLogger(__name__)

MAX_TELEGRAM_MESSAGE_LENGTH = 4000
MAX_INPUT_CHARACTERS = 8000


async def _send_long_message(message, text: str) -> None:
    for start in range(0, len(text), MAX_TELEGRAM_MESSAGE_LENGTH):
        chunk = text[start:start + MAX_TELEGRAM_MESSAGE_LENGTH]
        await message.reply_text(chunk)


async def newchat_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text(
            "Please use /newchat in a private conversation."
        )
        return

    try:
        session_factory = get_session_factory()

        await start_new_conversation(
            session_factory,
            user.id,
        )

        await message.reply_text(
            "✨ A new conversation has started.\n\n"
            "Your previous conversation is safely preserved. "
            "Let's start fresh!"
        )

    except ConversationAccessError:
        await message.reply_text(
            "Your Remique Zero account is not active. "
            "Please contact the administrator."
        )

    except Exception:
        logger.exception(
            "Unable to start new conversation for user %s",
            user.id,
        )

        await message.reply_text(
            "I couldn't start a new conversation. Please try again."
        )


async def ai_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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

    prompt = (message.text or "").strip()

    if not prompt:
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

        result: AIReply = await generate_ai_reply(
            session_factory,
            telegram_user_id=user.id,
            prompt=prompt,
            history=history,
        )

        try:
            await save_conversation_exchange(
                session_factory,
                telegram_user_id=user.id,
                conversation_id=conversation_id,
                prompt=prompt,
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
            "Unexpected AI handler error for Telegram user %s",
            user.id,
        )

        await message.reply_text(
            "Something went wrong while processing your message."
        )