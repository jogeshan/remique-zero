import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task

logger = logging.getLogger(__name__)

TASK_STATUSES = ("open", "done")

MAX_TITLE_LENGTH = 300
MAX_NOTES_LENGTH = 2000

# Short IDs are the first 8 characters of the UUID, as shown by
# /tasks. Prefix matching accepts anything from 6 characters up,
# mirroring the debt-ID resolution in the finance tools.
MIN_SHORT_ID_LENGTH = 6


async def create_task(
    session: AsyncSession,
    user_id: UUID,
    *,
    title: str,
    notes: str | None = None,
    due_at: datetime | None = None,
) -> Task:
    """Add one task to a user's personal task list."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A non-empty task title is required.")

    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)

    task = Task(
        user_id=user_id,
        title=title.strip()[:MAX_TITLE_LENGTH],
        notes=(notes.strip()[:MAX_NOTES_LENGTH] or None)
        if isinstance(notes, str)
        else None,
        status="open",
        due_at=due_at,
    )
    session.add(task)
    await session.commit()
    return task


async def list_tasks(
    session: AsyncSession,
    user_id: UUID,
    *,
    status: str = "open",
) -> list[Task]:
    """
    Return a user's tasks, oldest first (stable short IDs).

    status is "open", "done", or "all".
    """
    if status not in (*TASK_STATUSES, "all"):
        raise ValueError("status must be 'open', 'done', or 'all'.")

    query = (
        select(Task)
        .where(Task.user_id == user_id)
        .order_by(Task.created_at.asc(), Task.id.asc())
    )

    if status != "all":
        query = query.where(Task.status == status)

    return list(await session.scalars(query))


async def _get_task(
    session: AsyncSession,
    user_id: UUID,
    task_id: UUID,
) -> Task | None:
    """Fetch one of the user's tasks by ID, whatever its status."""
    return await session.scalar(
        select(Task).where(
            Task.id == task_id,
            Task.user_id == user_id,
        )
    )


async def resolve_task(
    session: AsyncSession,
    user_id: UUID,
    raw,
) -> Task:
    """
    Resolve a task reference to one of the user's open tasks.

    Accepts a full UUID (from list_tasks) or an unambiguous short
    prefix of one of the user's open task IDs (the short form
    shown by /tasks). Raises ValueError with a user-facing message
    when the reference is invalid, unknown, ambiguous, or points
    at a task that is no longer open.
    """
    if isinstance(raw, UUID):
        task_id = raw
        text = None
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip().lower()
        try:
            task_id = UUID(text)
        except ValueError:
            task_id = None
    else:
        raise ValueError("A valid task_id is required.")

    if task_id is not None:
        task = await _get_task(session, user_id, task_id)

        if task is None:
            raise ValueError(
                "That task was not found among your tasks."
            )

        if task.status != "open":
            raise ValueError("That task is already done.")

        return task

    if len(text) < MIN_SHORT_ID_LENGTH:
        raise ValueError("A valid task_id is required.")

    open_tasks = await list_tasks(session, user_id, status="open")
    matches = [
        task
        for task in open_tasks
        if str(task.id).startswith(text)
    ]

    if not matches:
        raise ValueError(
            "That task was not found among your open tasks."
        )

    if len(matches) > 1:
        raise ValueError(
            "That short ID matches more than one open task. "
            "Please use a longer ID from your task list."
        )

    return matches[0]


async def complete_task(
    session: AsyncSession,
    user_id: UUID,
    task_ref,
) -> Task:
    """Mark one of the user's open tasks as done."""
    task = await resolve_task(session, user_id, task_ref)

    task.status = "done"
    task.completed_at = datetime.now(timezone.utc)

    await session.commit()
    return task


async def delete_task(
    session: AsyncSession,
    user_id: UUID,
    task_ref,
) -> Task:
    """Delete one of the user's open tasks entirely."""
    task = await resolve_task(session, user_id, task_ref)

    await session.delete(task)
    await session.commit()
    return task
