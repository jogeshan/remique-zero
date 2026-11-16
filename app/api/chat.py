"""
Chat endpoints for the API (/api/v1/chat).

This is the mobile/web-app door into the same AI pipeline the
Telegram bot uses: the caller's API token identifies the user,
their linked Telegram account keys the conversation, and the
reply comes from ai_service.generate_ai_reply with the full
tool flow (reminders, money, tasks, memories) — then the
exchange is persisted exactly like a Telegram exchange, so
app and Telegram share one conversation history.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dashboard import get_current_api_user
from app.database import get_db_session, get_session_factory
from app.models import Conversation, ConversationMessage, TelegramAccount, User
from app.services.ai_service import (
    AIAccountInactive,
    AIConfigurationError,
    AIProviderError,
    AIQuotaExceeded,
    AIServiceError,
    generate_ai_reply,
)
from app.services.conversation_service import (
    get_active_conversation_context,
    save_conversation_exchange,
)

logger = logging.getLogger(__name__)

chat_router = APIRouter(prefix="/api/v1", tags=["chat-api"])

MAX_HISTORY_LIMIT = 100


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)


async def _telegram_user_id(session: AsyncSession, user: User) -> int:
    """The Telegram ID linked to this API user (the AI key)."""
    account = await session.scalar(
        select(TelegramAccount).where(TelegramAccount.user_id == user.id)
    )

    if account is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This account has no linked Telegram account, so "
                "chat is not available for it yet."
            ),
        )

    return account.telegram_user_id


@chat_router.post("/chat")
async def post_chat(
    payload: ChatRequest,
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    telegram_user_id = await _telegram_user_id(session, user)
    session_factory = get_session_factory()

    try:
        conversation_id, history = await get_active_conversation_context(
            session_factory,
            telegram_user_id,
        )

        result = await generate_ai_reply(
            session_factory,
            telegram_user_id=telegram_user_id,
            prompt=payload.message,
            history=history,
            conversation_id=conversation_id,
        )
    except AIAccountInactive:
        raise HTTPException(
            status_code=403,
            detail="Your Remique Zero account is not active.",
        )
    except AIQuotaExceeded:
        raise HTTPException(
            status_code=429,
            detail="You have reached your daily AI request limit.",
        )
    except AIConfigurationError:
        logger.error("Gemini API configuration is incomplete.")
        raise HTTPException(
            status_code=503,
            detail="The AI service is not configured correctly.",
        )
    except AIProviderError:
        raise HTTPException(
            status_code=502,
            detail="The AI service is temporarily unavailable.",
        )
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception(
            "API chat failed for user %s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Something went wrong while processing your message.",
        )

    try:
        await save_conversation_exchange(
            session_factory,
            telegram_user_id=telegram_user_id,
            conversation_id=conversation_id,
            prompt=payload.message,
            response=result.text,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
    except Exception:
        # The reply exists; losing the history write must not
        # fail the request (mirrors the Telegram handler, which
        # warns the user but still delivers the answer).
        logger.exception(
            "API chat reply generated but persistence failed for user %s",
            user.id,
        )

    return {"reply": result.text, "model": result.model}


@chat_router.get("/chat/history")
async def get_chat_history(
    limit: int = Query(default=50, ge=1, le=MAX_HISTORY_LIMIT),
    user: User = Depends(get_current_api_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Recent messages of the user's active conversation."""
    conversation = await session.scalar(
        select(Conversation)
        .where(
            Conversation.user_id == user.id,
            Conversation.is_active.is_(True),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(1)
    )

    if conversation is None:
        return {"messages": []}

    rows = await session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
        .order_by(
            ConversationMessage.created_at.desc(),
            ConversationMessage.id.desc(),
        )
        .limit(limit)
    )
    messages = list(rows)
    messages.reverse()

    return {
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "created_at": (
                    message.created_at.isoformat()
                    if message.created_at is not None
                    else None
                ),
            }
            for message in messages
        ]
    }
