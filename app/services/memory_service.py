import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TelegramAccount, User, UserMemory

logger = logging.getLogger(__name__)

# Hard cap on stored memories per user. When a save would exceed the
# cap, the oldest memories are deleted so the total stays at the cap.
# This keeps per-user storage (and prompt size) bounded without any
# background cleanup job.
MAX_MEMORIES_PER_USER = 200

DEFAULT_CATEGORY = "fact"
MAX_CATEGORY_LENGTH = 50


async def get_active_user_by_telegram_id(
    session: AsyncSession,
    telegram_user_id: int,
) -> User | None:
    """
    Resolve the active user for a Telegram ID.

    Mirrors conversation_service._get_active_user, but returns None
    instead of raising so tool callers can report a plain error.
    """
    return await session.scalar(
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


async def save_memory(
    session: AsyncSession,
    user_id: UUID,
    content: str,
    category: str = DEFAULT_CATEGORY,
) -> UserMemory:
    """Store one memory for a user, enforcing the per-user cap."""
    memory = UserMemory(
        user_id=user_id,
        content=content,
        category=(category or DEFAULT_CATEGORY)[:MAX_CATEGORY_LENGTH],
    )
    session.add(memory)
    await session.flush()

    # Cap enforcement: count after insert and delete the oldest
    # memories until the user is back at the cap.
    total = await session.scalar(
        select(func.count())
        .select_from(UserMemory)
        .where(UserMemory.user_id == user_id)
    )

    excess = (total or 0) - MAX_MEMORIES_PER_USER
    if excess > 0:
        oldest = await session.scalars(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.asc(), UserMemory.id.asc())
            .limit(excess)
        )
        for stale in oldest:
            await session.delete(stale)

    await session.commit()
    return memory


async def list_memories(
    session: AsyncSession,
    user_id: UUID,
    limit: int = 50,
) -> list[UserMemory]:
    """Return a user's memories, newest first."""
    result = await session.scalars(
        select(UserMemory)
        .where(UserMemory.user_id == user_id)
        .order_by(UserMemory.created_at.desc(), UserMemory.id.desc())
        .limit(limit)
    )
    return list(result)


async def get_memories_for_context(
    session: AsyncSession,
    user_id: UUID,
    limit: int = 20,
) -> list[UserMemory]:
    """
    Return the latest memories for prompt context.

    Fetched newest-first, then reversed so the prompt reads in
    chronological order.
    """
    memories = await list_memories(session, user_id, limit=limit)
    memories.reverse()
    return memories


async def delete_memory(
    session: AsyncSession,
    user_id: UUID,
    memory_id: UUID,
) -> bool:
    """
    Delete one memory owned by the user.

    The owner check is part of the lookup, so a user can never delete
    another user's memory by guessing its ID.
    """
    memory = await session.scalar(
        select(UserMemory).where(
            UserMemory.id == memory_id,
            UserMemory.user_id == user_id,
        )
    )

    if memory is None:
        return False

    await session.delete(memory)
    await session.commit()
    return True


def format_memories_for_prompt(
    memories: list[UserMemory],
    max_chars: int = 2000,
) -> str:
    """
    Render memories as a compact bullet list for the system prompt.

    Whole bullets are added until the character budget runs out, so
    the output never ends mid-fact; a single oversized bullet is
    hard-truncated with an ellipsis. Returns "" for no memories.
    """
    if not memories:
        return ""

    lines: list[str] = []
    used = 0

    for memory in memories:
        content = " ".join((memory.content or "").split())
        if not content:
            continue

        category = (memory.category or DEFAULT_CATEGORY).strip()
        line = f"- [{category}] {content}" if category else f"- {content}"

        # +1 accounts for the newline joining this line to the rest.
        if used + len(line) + 1 > max_chars:
            if not lines:
                # Even the first bullet exceeds the budget: truncate it.
                lines.append(line[: max_chars - 1].rstrip() + "…")
            break

        lines.append(line)
        used += len(line) + 1

    return "\n".join(lines)
