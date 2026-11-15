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

# Context compression: once a conversation has more than
# COMPRESSION_THRESHOLD messages beyond those already folded into
# its rolling summary, the oldest COMPRESSION_CHUNK_SIZE of them
# are summarized and folded in. The model still receives the
# latest SUMMARY_RECENT_MESSAGES unsummarized messages verbatim.
SUMMARY_RECENT_MESSAGES = 20
COMPRESSION_THRESHOLD = 40
COMPRESSION_CHUNK_SIZE = 20
SUMMARY_MAX_CHARACTERS = 4000

SUMMARY_INSTRUCTION = (
    "Summarize the following conversation between a user and "
    "their assistant, preserving facts, preferences, decisions "
    "and open threads. Be concise."
)


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


def should_compress(
    total_messages: int,
    summarized_count: int,
    threshold: int = COMPRESSION_THRESHOLD,
) -> bool:
    """
    True when a conversation has more than `threshold` messages
    that have not yet been folded into its rolling summary.
    """
    return (total_messages - summarized_count) > threshold


def build_summary_prompt(
    previous_summary: str | None,
    chunk: list[dict],
) -> str:
    """
    Build the summarization prompt for one chunk of messages.

    When a previous summary exists it is included, so the model
    returns ONE updated summary that merges the old summary with
    the new chunk instead of a fragment.
    """
    lines = [SUMMARY_INSTRUCTION, ""]

    if previous_summary:
        lines.append("Summary of the earlier part of the conversation:")
        lines.append(previous_summary.strip())
        lines.append("")
        lines.append("New messages to fold into that summary:")
    else:
        lines.append("Conversation so far:")

    for item in chunk:
        role = item.get("role")
        text = (item.get("text") or "").strip()

        if role not in ("user", "model") or not text:
            continue

        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {text}")

    lines.append("")
    lines.append("Updated summary:")

    return "\n".join(lines)


def cap_summary(
    text: str,
    max_characters: int = SUMMARY_MAX_CHARACTERS,
) -> str:
    """Trim a summary to the storage cap, on a word boundary."""
    text = (text or "").strip()

    if len(text) <= max_characters:
        return text

    truncated = text[:max_characters].rsplit(" ", 1)[0].rstrip()
    return (truncated or text[:max_characters]) + "…"


async def get_history_with_summary(
    session,
    conversation: Conversation,
) -> tuple[str | None, list[dict]]:
    """
    Return (summary, recent messages) for a conversation.

    Recent messages are the latest SUMMARY_RECENT_MESSAGES
    messages that have NOT been folded into the summary, in
    chronological order, in the {"role", "text"} shape the AI
    layer consumes.
    """
    summarized = conversation.summary_message_count or 0

    result = await session.execute(
        select(
            ConversationMessage.role,
            ConversationMessage.content,
        )
        .where(
            ConversationMessage.conversation_id == conversation.id,
        )
        .order_by(
            ConversationMessage.created_at.asc(),
            ConversationMessage.id.asc(),
        )
        .offset(summarized)
    )

    rows = list(result.all())
    rows = rows[-SUMMARY_RECENT_MESSAGES:]

    history = [
        {"role": row.role, "text": row.content}
        for row in rows
    ]

    return conversation.summary, history


async def count_conversation_messages(
    session,
    conversation_id: UUID,
) -> int:
    """Total stored messages in a conversation, summarized or not."""
    count = await session.scalar(
        select(func.count())
        .select_from(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
        )
    )
    return count or 0


async def get_oldest_unsummarized_messages(
    session,
    conversation: Conversation,
    limit: int = COMPRESSION_CHUNK_SIZE,
) -> list[dict]:
    """
    The oldest `limit` messages not yet folded into the summary,
    in chronological order, as {"role", "text"} dicts.
    """
    summarized = conversation.summary_message_count or 0

    result = await session.execute(
        select(
            ConversationMessage.role,
            ConversationMessage.content,
        )
        .where(
            ConversationMessage.conversation_id == conversation.id,
        )
        .order_by(
            ConversationMessage.created_at.asc(),
            ConversationMessage.id.asc(),
        )
        .offset(summarized)
        .limit(limit)
    )

    return [
        {"role": row.role, "text": row.content}
        for row in result.all()
    ]


async def save_conversation_summary(
    session,
    conversation: Conversation,
    summary_text: str,
    folded_count: int,
) -> None:
    """
    Persist a rolling summary and advance the folded-message
    counter by the number of messages folded into it.
    """
    conversation.summary = summary_text
    conversation.summary_message_count = (
        conversation.summary_message_count or 0
    ) + folded_count

    await session.commit()


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