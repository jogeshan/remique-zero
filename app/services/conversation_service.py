import logging
from uuid import UUID

from sqlalchemy import func, select

from app.models import (
    Conversation,
    ConversationMessage,
    TelegramAccount,
    User,
)

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10


class ConversationAccessError(Exception):
    """The Telegram account cannot access conversations."""


async def _get_active_user(session, telegram_user_id: int) -> User:
    result = await session.execute(
        select(User)
        .join(
            TelegramAccount,
            TelegramAccount.user_id == User.id,
        )
        .where(
            TelegramAccount.telegram_user_id == telegram_user_id,
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )

    user = result.scalar_one_or_none()

    if user is None:
        raise ConversationAccessError(
            "Your Remique Zero account is not active."
        )

    return user


async def _lock_user(session, user_id: UUID) -> None:
    lock_key = user_id.int & 0x7FFFFFFFFFFFFFFF

    await session.execute(
        select(func.pg_advisory_xact_lock(lock_key))
    )


async def get_active_conversation_context(
    session_factory,
    telegram_user_id: int,
) -> tuple[UUID, list[dict]]:
    """
    Return the active conversation ID and its latest messages.

    Creates an active conversation automatically when necessary.
    """

    async with session_factory() as session:
        async with session.begin():
            user = await _get_active_user(
                session,
                telegram_user_id,
            )

            await _lock_user(session, user.id)

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
                conversation = Conversation(
                    user_id=user.id,
                    title="New conversation",
                    is_active=True,
                )

                session.add(conversation)
                await session.flush()

            conversation_id = conversation.id

        result = await session.execute(
            select(
                ConversationMessage.role,
                ConversationMessage.content,
            )
            .where(
                ConversationMessage.conversation_id == conversation_id,
            )
            .order_by(
                ConversationMessage.created_at.desc(),
                ConversationMessage.id.desc(),
            )
            .limit(MAX_HISTORY_MESSAGES)
        )

        messages = list(result.all())
        messages.reverse()

        history = [
            {
                "role": row.role,
                "text": row.content,
            }
            for row in messages
        ]

        return conversation_id, history


async def save_conversation_exchange(
    session_factory,
    *,
    telegram_user_id: int,
    conversation_id: UUID,
    prompt: str,
    response: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Persist one successful user/assistant exchange."""

    async with session_factory() as session:
        async with session.begin():
            user = await _get_active_user(
                session,
                telegram_user_id,
            )

            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user.id,
                )
            )

            if conversation is None:
                raise ConversationAccessError(
                    "Conversation not found."
                )

            if conversation.title == "New conversation":
                conversation.title = prompt[:150]

            session.add_all(
                [
                    ConversationMessage(
                        conversation_id=conversation.id,
                        role="user",
                        content=prompt,
                        model=None,
                        input_tokens=0,
                        output_tokens=0,
                    ),
                    ConversationMessage(
                        conversation_id=conversation.id,
                        role="model",
                        content=response,
                        model=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ),
                ]
            )

            conversation.updated_at = func.now()


async def start_new_conversation(
    session_factory,
    telegram_user_id: int,
) -> UUID:
    """
    Archive the current active conversation and create another one.

    Previous conversations and messages are preserved.
    """

    async with session_factory() as session:
        async with session.begin():
            user = await _get_active_user(
                session,
                telegram_user_id,
            )

            await _lock_user(session, user.id)

            active = await session.scalar(
                select(Conversation).where(
                    Conversation.user_id == user.id,
                    Conversation.is_active.is_(True),
                )
            )

            if active is not None:
                active.is_active = False

            conversation = Conversation(
                user_id=user.id,
                title="New conversation",
                is_active=True,
            )

            session.add(conversation)
            await session.flush()

            return conversation.id