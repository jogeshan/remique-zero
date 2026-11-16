import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.models import AdminAuditLog, TelegramAccount, User, UserSetting
from app.services import (
    finance_service,
    memory_service,
    reminder_service,
    task_service,
)
from app.services.admin_service import (
    create_admin_invite,
    get_user_statistics,
    revoke_admin_invite,
)
from app.services.user_management_service import (
    change_user_role,
    set_user_status,
)

logger = logging.getLogger(__name__)

# Gemini receives these declarations. Python remains responsible for
# authorization, validation, and executing every operation.

TOOL_DECLARATIONS = [
    {
        "name": "create_invite",
        "description": (
            "Create a registration invitation code. "
            "Available to admins and super admins."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "max_uses": {
                    "type": "integer",
                    "description": "Maximum number of registrations. Defaults to 1.",
                    "minimum": 1,
                    "maximum": 100,
                },
                "expires_in_days": {
                    "type": "integer",
                    "description": (
                        "Number of days until expiration. "
                        "Use null or omit for no expiration."
                    ),
                    "minimum": 1,
                    "maximum": 365,
                },
            },
        },
    },
    {
        "name": "revoke_invite",
        "description": (
            "Revoke an existing invitation code. "
            "Available to admins and super admins."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The complete invitation code to revoke.",
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_user_statistics",
        "description": (
            "Get total, active, disabled, and administrator user counts. "
            "Available to admins and super admins."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_users",
        "description": (
            "List registered users with their Telegram IDs, usernames, "
            "display names, roles, and account statuses. "
            "Available to admins and super admins."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum users to return. Defaults to 20.",
                    "minimum": 1,
                    "maximum": 50,
                }
            },
        },
    },
    {
        "name": "set_user_status",
        "description": (
            "Enable or disable a regular user's account. "
            "Admins can manage regular users. Super admins can manage "
            "admins and regular users. No administrator can disable "
            "a super-admin account or their own account."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "target_telegram_id": {
                    "type": "integer",
                    "description": "Telegram numeric user ID of the target.",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "disabled"],
                },
            },
            "required": ["target_telegram_id", "status"],
        },
    },
    {
        "name": "change_user_role",
        "description": (
            "Promote a regular user to admin or demote an admin to regular "
            "user. Super-admin permission is required. A super admin "
            "cannot change their own role or another super-admin's role."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "target_telegram_id": {
                    "type": "integer",
                    "description": "Telegram numeric user ID of the target.",
                },
                "role": {
                    "type": "string",
                    "enum": ["user", "admin"],
                },
            },
            "required": ["target_telegram_id", "role"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Save a fact or preference to the calling user's own "
            "long-term memory. Available to every user, for their own "
            "memory only. Use it when the user asks to be remembered "
            "(for example 'remember that ...')."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact or preference to remember.",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Short category such as 'fact', 'preference', "
                        "'project' or 'instruction'. Defaults to 'fact'."
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall_memories",
        "description": (
            "List the calling user's own saved memories. Available to "
            "every user, for their own memory only. Pass a query to "
            "search older memories by keyword — facts beyond the most "
            "recent ones are only reachable this way."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum memories to return. Defaults to 20.",
                    "minimum": 1,
                    "maximum": 50,
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional keyword to search memory contents "
                        "(case-insensitive). Use it when the user asks "
                        "about something specific that may be an older "
                        "memory."
                    ),
                },
            },
        },
    },
    {
        "name": "forget_memory",
        "description": (
            "Delete one of the calling user's own saved memories by "
            "its memory ID (from recall_memories). A user can only "
            "delete their own memories."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The memory ID returned by recall_memories.",
                }
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "create_reminder",
        "description": (
            "Create a reminder for the calling user. Available to "
            "every user, for their own reminders only. The reminder "
            "is delivered as a Telegram message at the requested "
            "time."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "What the user wants to be reminded about.",
                },
                "first_run_at": {
                    "type": "string",
                    "description": (
                        "When the reminder first fires, as an ISO 8601 "
                        "timestamp WITH a timezone offset, resolved in "
                        "the user's timezone (for example "
                        "2026-11-16T09:00:00+06:00)."
                    ),
                },
                "recurrence": {
                    "type": "string",
                    "enum": ["daily", "weekly", "weekdays"],
                    "description": (
                        "Optional repeat schedule. Omit for a "
                        "one-time reminder."
                    ),
                },
            },
            "required": ["title", "first_run_at"],
        },
    },
    {
        "name": "list_reminders",
        "description": (
            "List the calling user's active reminders with their "
            "next delivery times. Available to every user, for "
            "their own reminders only."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "cancel_reminder",
        "description": (
            "Cancel one of the calling user's reminders by its "
            "reminder ID (from list_reminders). A user can only "
            "cancel their own reminders."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "string",
                    "description": "The reminder ID returned by list_reminders.",
                }
            },
            "required": ["reminder_id"],
        },
    },
    {
        "name": "snooze_reminder",
        "description": (
            "Delay the next delivery of one of the calling user's "
            "reminders by a number of minutes. A user can only "
            "snooze their own reminders."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "string",
                    "description": "The reminder ID returned by list_reminders.",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Minutes to delay by. Defaults to 10.",
                    "minimum": 1,
                    "maximum": 10080,
                },
            },
            "required": ["reminder_id"],
        },
    },
    {
        "name": "add_transaction",
        "description": (
            "Record an income or expense transaction for the "
            "calling user. Available to every user, for their own "
            "finances only. Use it whenever the user mentions "
            "spending or earning money. Amounts are in BDT (taka) "
            "unless the user says otherwise."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["income", "expense"],
                    "description": "Whether money came in or went out.",
                },
                "amount": {
                    "type": "number",
                    "description": "The amount, as a positive number.",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Choose from context. Expenses: Food, "
                        "Transport, Shopping, Bills, Rent, Health, "
                        "Entertainment, Education, Family, Other. "
                        "Income: Salary, Business, Freelance, Gift, "
                        "Other."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": "Short note, e.g. what was bought.",
                },
                "occurred_at": {
                    "type": "string",
                    "description": (
                        "Optional ISO 8601 timestamp of when the "
                        "transaction happened. Omit for right now."
                    ),
                },
            },
            "required": ["type", "amount", "category"],
        },
    },
    {
        "name": "get_finance_summary",
        "description": (
            "Get the calling user's income/expense totals and "
            "per-category breakdown for a period. Available to "
            "every user, for their own finances only."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": [
                        "today",
                        "yesterday",
                        "week",
                        "month",
                        "last_month",
                    ],
                    "description": "Defaults to month.",
                },
            },
        },
    },
    {
        "name": "list_transactions",
        "description": (
            "List the calling user's recent transactions for a "
            "period, optionally filtered by type or category. "
            "Available to every user, for their own finances only."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": [
                        "today",
                        "yesterday",
                        "week",
                        "month",
                        "last_month",
                    ],
                    "description": "Defaults to month.",
                },
                "type": {
                    "type": "string",
                    "enum": ["income", "expense"],
                    "description": "Optional filter.",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows. Defaults to 20.",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
        },
    },
    {
        "name": "add_debt",
        "description": (
            "Record money the calling user borrowed from or lent "
            "to someone. 'borrowed' means the user owes the other "
            "person; 'lent' means the other person owes the user. "
            "Available to every user, for their own debts only."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["borrowed", "lent"],
                },
                "kind": {
                    "type": "string",
                    "enum": ["debt", "loan"],
                    "description": "Defaults to debt.",
                },
                "counterparty": {
                    "type": "string",
                    "description": "The other person's name.",
                },
                "amount": {
                    "type": "number",
                    "description": "The amount, as a positive number.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note.",
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional due date, YYYY-MM-DD.",
                },
            },
            "required": ["direction", "counterparty", "amount"],
        },
    },
    {
        "name": "record_debt_payment",
        "description": (
            "Record a (partial) repayment against one of the "
            "calling user's open debts, by debt ID from list_debts. "
            "Works for money the user pays back and for money "
            "repaid to the user."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "debt_id": {
                    "type": "string",
                    "description": "The debt ID returned by list_debts.",
                },
                "amount": {
                    "type": "number",
                    "description": "The amount repaid, as a positive number.",
                },
            },
            "required": ["debt_id", "amount"],
        },
    },
    {
        "name": "list_debts",
        "description": (
            "List the calling user's open debts and loans with "
            "totals: how much they owe and how much is owed to "
            "them. Available to every user, for their own debts "
            "only."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["borrowed", "lent"],
                    "description": "Optional filter.",
                },
            },
        },
    },
    {
        "name": "set_expense_reminders",
        "description": (
            "Turn the calling user's daily expense check-in "
            "reminders on or off. When on, the bot nudges the user "
            "to log what they spent or earned, several times a day."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "True to turn check-ins on, false to turn them off.",
                },
                "times_per_day": {
                    "type": "integer",
                    "description": "Check-ins per day, 1 to 8. Defaults to 4.",
                    "minimum": 1,
                    "maximum": 8,
                },
            },
            "required": ["enabled"],
        },
    },
    {
        "name": "get_daily_overview",
        "description": (
            "Show the calling user's money overview for a day: "
            "expenses and income with category breakdowns, and "
            "the day's borrowing/lending activity. Defaults to "
            "yesterday."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "enum": ["yesterday", "today"],
                    "description": "Defaults to yesterday.",
                },
            },
        },
    },
    {
        "name": "set_daily_overview",
        "description": (
            "Turn the calling user's automatic morning overview "
            "of yesterday's money on or off. When on, it is "
            "delivered daily at the chosen local time (default "
            "08:00)."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "True to turn the overview on, false to turn it off.",
                },
                "time": {
                    "type": "string",
                    "description": "Local delivery time, HH:MM. Defaults to 08:00.",
                },
            },
            "required": ["enabled"],
        },
    },
    {
        "name": "create_task",
        "description": (
            "Add an item to the calling user's personal task "
            "list (to-do list). Tasks are NOT reminders: nothing "
            "is delivered at a time unless the user also asks "
            "for a reminder. Available to every user, for their "
            "own list only."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "What needs to be done.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional extra detail about the task.",
                },
                "due_at": {
                    "type": "string",
                    "description": (
                        "Optional due date/time as ISO 8601 with "
                        "a timezone offset (e.g. "
                        "2026-11-16T09:00:00+06:00). Informational "
                        "only — it does not schedule a reminder."
                    ),
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "List the calling user's personal tasks. Defaults "
            "to open tasks; use status to see done tasks or all."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "done", "all"],
                    "description": "Defaults to open.",
                },
            },
        },
    },
    {
        "name": "complete_task",
        "description": (
            "Mark one of the calling user's open tasks as done, "
            "by task ID from list_tasks (full ID or the short "
            "ID shown in the list)."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID returned by list_tasks.",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_task",
        "description": (
            "Delete one of the calling user's open tasks "
            "entirely, by task ID from list_tasks (full ID or "
            "the short ID shown in the list)."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID returned by list_tasks.",
                },
            },
            "required": ["task_id"],
        },
    },
]


def _integer(
    value,
    name: str,
    *,
    minimum: int = 1,
    maximum: int = 2_147_483_647,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")

    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer.") from None

    if result < minimum or result > maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}."
        )

    return result


async def _memory_actor(session, telegram_user_id: int):
    """
    Resolve the acting user for the personal-memory tools.

    Memory tools only ever operate on the caller's own data, so the
    user always comes from the authenticated Telegram ID — tool
    arguments can never name another user.
    """
    user = await memory_service.get_active_user_by_telegram_id(
        session,
        telegram_user_id,
    )

    if user is None:
        return None, {
            "success": False,
            "error": "Your Remique Zero account is not active.",
        }

    return user, None


def _memory_to_dict(memory) -> dict:
    return {
        "id": str(memory.id),
        "content": memory.content,
        "category": memory.category,
        "created_at": (
            memory.created_at.isoformat()
            if memory.created_at
            else None
        ),
    }


async def _reminder_actor(session, telegram_user_id: int):
    """
    Resolve the acting user for the reminder tools.

    Reminders are personal data: the tools only ever operate on
    the caller's own reminders, resolved from the authenticated
    Telegram ID — arguments can never name another user.
    """
    return await _memory_actor(session, telegram_user_id)


async def _user_timezone_name(session, user_id) -> str:
    """Return the user's configured timezone name (or the default)."""
    tz_name = await session.scalar(
        select(UserSetting.timezone).where(
            UserSetting.user_id == user_id
        )
    )
    return tz_name or settings.default_timezone


def _format_local_time(value: datetime, tz_name: str) -> str:
    """Render a UTC datetime in the user's timezone for display."""
    local = value.astimezone(ZoneInfo(tz_name))
    return local.strftime("%a %d %b %Y, %H:%M") + f" ({tz_name})"


def _reminder_to_dict(reminder, next_at, tz_name: str) -> dict:
    return {
        "id": str(reminder.id),
        "title": reminder.title,
        "recurrence": reminder_service.recurrence_label(
            reminder.recurrence_rule
        ),
        "next_at": next_at.isoformat() if next_at else None,
        "next_at_local": (
            _format_local_time(next_at, tz_name) if next_at else None
        ),
    }


async def _resolve_reminder_id(session, user_id, raw) -> UUID | None:
    """
    Resolve a reminder ID argument to a UUID owned by the user.

    Accepts the full UUID from list_reminders, or an unambiguous
    short prefix of one of the user's active reminder IDs (the
    short form shown by the /reminders command).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("A valid reminder_id is required.")

    text = raw.strip()

    try:
        return UUID(text)
    except ValueError:
        pass

    if len(text) >= 6:
        pairs = await reminder_service.list_active_reminders(
            session,
            user_id,
        )
        matches = [
            reminder
            for reminder, _ in pairs
            if str(reminder.id).startswith(text.lower())
        ]
        if len(matches) == 1:
            return matches[0].id

    return None


async def _finance_actor(session, telegram_user_id: int):
    """
    Resolve the acting user for the finance tools.

    Finances are personal data: the tools only ever operate on
    the caller's own transactions and debts, resolved from the
    authenticated Telegram ID — arguments can never name another
    user.
    """
    return await _memory_actor(session, telegram_user_id)


def _transaction_to_dict(transaction, tz_name: str) -> dict:
    occurred = transaction.occurred_at
    return {
        "id": str(transaction.id),
        "type": transaction.type,
        "amount": float(transaction.amount),
        "currency": transaction.currency,
        "category": transaction.category,
        "note": transaction.note,
        "occurred_at": occurred.isoformat() if occurred else None,
        "occurred_at_local": (
            _format_local_time(occurred, tz_name) if occurred else None
        ),
    }


def _debt_to_dict(debt) -> dict:
    return {
        "id": str(debt.id),
        "short_id": str(debt.id)[:8],
        "direction": debt.direction,
        "kind": debt.kind,
        "counterparty": debt.counterparty,
        "principal": float(debt.principal),
        "remaining": float(debt.remaining),
        "currency": finance_service.DEFAULT_CURRENCY,
        "note": debt.note,
        "due_date": debt.due_date.isoformat() if debt.due_date else None,
        "status": debt.status,
    }


async def _resolve_debt_id(session, user_id, raw) -> UUID | None:
    """
    Resolve a debt ID argument to a UUID owned by the user.

    Accepts the full UUID from list_debts, or an unambiguous short
    prefix of one of the user's open debt IDs (the short form
    shown by the /debts command).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("A valid debt_id is required.")

    text = raw.strip()

    try:
        return UUID(text)
    except ValueError:
        pass

    if len(text) >= 6:
        debts = await finance_service.list_debts(session, user_id)
        matches = [
            debt
            for debt in debts
            if str(debt.id).startswith(text.lower())
        ]
        if len(matches) == 1:
            return matches[0].id

    return None


def _parse_tool_datetime(raw, tz_name: str) -> datetime:
    """Parse an ISO 8601 tool timestamp; naive means the user's tz."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("A valid ISO 8601 timestamp is required.")

    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            "Timestamps must be ISO 8601 (e.g. 2026-11-16T09:00:00+06:00)."
        ) from None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))

    return parsed.astimezone(timezone.utc)


async def _task_actor(session, telegram_user_id: int):
    """
    Resolve the acting user for the task-list tools.

    Tasks are personal data: the tools only ever operate on the
    caller's own list, resolved from the authenticated Telegram
    ID — arguments can never name another user.
    """
    return await _memory_actor(session, telegram_user_id)


def _task_to_dict(task, tz_name: str) -> dict:
    due_at = task.due_at
    completed_at = task.completed_at
    created_at = task.created_at
    return {
        "id": str(task.id),
        "short_id": str(task.id)[:8],
        "title": task.title,
        "notes": task.notes,
        "status": task.status,
        "due_at": due_at.isoformat() if due_at else None,
        "due_at_local": (
            _format_local_time(due_at, tz_name) if due_at else None
        ),
        "created_at": (
            created_at.isoformat() if created_at else None
        ),
        "completed_at": (
            completed_at.isoformat() if completed_at else None
        ),
    }


async def _log_tool_execution(
    session_factory,
    *,
    telegram_user_id: int,
    tool_name: str,
    result: dict,
) -> None:
    """
    Write one AdminAuditLog row for a completed AI tool execution.

    Runs in its own session after the tool has finished. Only the
    outcome is recorded — never arguments or results, which may
    contain invitation codes or other secrets. A failure here only
    logs a warning; it must never change the tool result.
    """
    try:
        async with session_factory() as session:
            actor_user_id = await session.scalar(
                select(TelegramAccount.user_id).where(
                    TelegramAccount.telegram_user_id == telegram_user_id
                )
            )

            session.add(
                AdminAuditLog(
                    actor_user_id=actor_user_id,
                    action=f"ai_tool.{tool_name}",
                    metadata_json={
                        "success": bool(result.get("success")),
                        "error": result.get("error"),
                    },
                )
            )

            await session.commit()

    except Exception:
        logger.warning(
            "Failed to write AI tool audit log: tool=%s actor=%s",
            tool_name,
            telegram_user_id,
            exc_info=True,
        )


async def execute_ai_tool(
    session_factory,
    *,
    telegram_user_id: int,
    tool_name: str,
    arguments: dict | None,
) -> dict:
    """
    Execute one Gemini-requested tool using the authenticated Telegram ID.

    Gemini is never allowed to supply or override the acting user's ID.
    Existing backend services enforce all permissions. Every outcome
    (success or failure) is recorded in the admin audit log.
    """
    result = await _execute_ai_tool(
        session_factory,
        telegram_user_id=telegram_user_id,
        tool_name=tool_name,
        arguments=arguments,
    )

    await _log_tool_execution(
        session_factory,
        telegram_user_id=telegram_user_id,
        tool_name=tool_name,
        result=result,
    )

    return result


async def _execute_ai_tool(
    session_factory,
    *,
    telegram_user_id: int,
    tool_name: str,
    arguments: dict | None,
) -> dict:
    arguments = arguments or {}

    if not isinstance(arguments, dict):
        return {
            "success": False,
            "error": "Tool arguments must be an object.",
        }

    try:
        async with session_factory() as session:

            if tool_name == "create_invite":
                max_uses = _integer(
                    arguments.get("max_uses", 1),
                    "max_uses",
                    minimum=1,
                    maximum=100,
                )

                expires_in_days = arguments.get("expires_in_days")
                if expires_in_days is not None:
                    expires_in_days = _integer(
                        expires_in_days,
                        "expires_in_days",
                        minimum=1,
                        maximum=365,
                    )

                invite = await create_admin_invite(
                    session,
                    telegram_user_id=telegram_user_id,
                    max_uses=max_uses,
                    expires_in_days=expires_in_days,
                )

                return {
                    "success": True,
                    "message": "Invitation created successfully.",
                    "code": invite.code,
                    "max_uses": invite.max_uses,
                    "expires_at": (
                        invite.expires_at.isoformat()
                        if invite.expires_at
                        else None
                    ),
                }

            if tool_name == "revoke_invite":
                code = arguments.get("code")

                if not isinstance(code, str) or not code.strip():
                    raise ValueError("A valid invitation code is required.")

                revoked = await revoke_admin_invite(
                    session,
                    telegram_user_id=telegram_user_id,
                    code=code.strip(),
                )

                if not revoked:
                    return {
                        "success": False,
                        "error": (
                            "Invitation was not found or was already revoked."
                        ),
                        "message": (
                            "Invitation was not found or was already revoked."
                        ),
                    }

                return {
                    "success": True,
                    "message": "Invitation revoked successfully.",
                }

            if tool_name == "get_user_statistics":
                stats = await get_user_statistics(
                    session,
                    telegram_user_id,
                )

                return {
                    "success": True,
                    "statistics": stats,
                }

            if tool_name == "list_users":
                from app.services.admin_service import get_admin

                admin = await get_admin(session, telegram_user_id)
                if admin is None:
                    raise PermissionError(
                        "Administrator access required."
                    )

                limit = _integer(
                    arguments.get("limit", 20),
                    "limit",
                    minimum=1,
                    maximum=50,
                )

                result = await session.execute(
                    select(User, TelegramAccount)
                    .join(
                        TelegramAccount,
                        TelegramAccount.user_id == User.id,
                    )
                    .where(User.deleted_at.is_(None))
                    .order_by(User.created_at.desc())
                    .limit(limit)
                )

                users = []
                for user, account in result.all():
                    users.append(
                        {
                            "telegram_user_id": account.telegram_user_id,
                            "username": account.username,
                            "display_name": user.display_name,
                            "role": user.role,
                            "status": user.status,
                            "created_at": (
                                user.created_at.isoformat()
                                if user.created_at
                                else None
                            ),
                        }
                    )

                return {
                    "success": True,
                    "count": len(users),
                    "users": users,
                }

            if tool_name == "set_user_status":
                target_id = _integer(
                    arguments.get("target_telegram_id"),
                    "target_telegram_id",
                )

                status = arguments.get("status")
                if status not in ("active", "disabled"):
                    raise ValueError(
                        "status must be 'active' or 'disabled'."
                    )

                changed = await set_user_status(
                    session,
                    actor_telegram_id=telegram_user_id,
                    target_telegram_id=target_id,
                    new_status=status,
                )

                if changed:
                    return {
                        "success": True,
                        "message": f"User account {status} successfully.",
                        "target_telegram_id": target_id,
                        "status": status,
                    }

                no_change = (
                    "No account change was made. The user may not exist "
                    "or may already have this status."
                )

                return {
                    "success": False,
                    "error": no_change,
                    "message": no_change,
                    "target_telegram_id": target_id,
                    "status": status,
                }

            if tool_name == "change_user_role":
                target_id = _integer(
                    arguments.get("target_telegram_id"),
                    "target_telegram_id",
                )

                role = arguments.get("role")
                if role not in ("user", "admin"):
                    raise ValueError(
                        "role must be 'user' or 'admin'."
                    )

                changed = await change_user_role(
                    session,
                    actor_telegram_id=telegram_user_id,
                    target_telegram_id=target_id,
                    new_role=role,
                )

                if changed:
                    return {
                        "success": True,
                        "message": f"User role changed to {role} successfully.",
                        "target_telegram_id": target_id,
                        "role": role,
                    }

                no_change = (
                    "No role change was made. The user may not exist "
                    "or may already have this role."
                )

                return {
                    "success": False,
                    "error": no_change,
                    "message": no_change,
                    "target_telegram_id": target_id,
                    "role": role,
                }

            if tool_name == "remember_fact":
                content = arguments.get("content")

                if not isinstance(content, str) or not content.strip():
                    raise ValueError("A non-empty fact is required.")

                category = arguments.get("category") or "fact"
                if not isinstance(category, str):
                    raise ValueError("category must be a string.")

                user, error = await _memory_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                memory = await memory_service.save_memory(
                    session,
                    user.id,
                    content.strip(),
                    category.strip() or "fact",
                )

                return {
                    "success": True,
                    "message": "Memory saved.",
                    "memory": _memory_to_dict(memory),
                }

            if tool_name == "recall_memories":
                limit = _integer(
                    arguments.get("limit", 20),
                    "limit",
                    minimum=1,
                    maximum=50,
                )

                user, error = await _memory_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                query = arguments.get("query")

                if isinstance(query, str) and query.strip():
                    memories = await memory_service.search_memories(
                        session,
                        user.id,
                        query,
                        limit=limit,
                    )
                else:
                    memories = await memory_service.list_memories(
                        session,
                        user.id,
                        limit=limit,
                    )

                return {
                    "success": True,
                    "count": len(memories),
                    "memories": [
                        _memory_to_dict(memory) for memory in memories
                    ],
                }

            if tool_name == "forget_memory":
                raw_id = arguments.get("memory_id")

                if not isinstance(raw_id, str) or not raw_id.strip():
                    raise ValueError("A valid memory_id is required.")

                try:
                    memory_id = UUID(raw_id.strip())
                except ValueError:
                    raise ValueError(
                        "memory_id must be a valid memory ID."
                    ) from None

                user, error = await _memory_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                deleted = await memory_service.delete_memory(
                    session,
                    user.id,
                    memory_id,
                )

                if not deleted:
                    return {
                        "success": False,
                        "message": (
                            "Memory was not found among your memories."
                        ),
                    }

                return {
                    "success": True,
                    "message": "Memory deleted.",
                }

            if tool_name == "create_reminder":
                title = arguments.get("title")

                if not isinstance(title, str) or not title.strip():
                    raise ValueError(
                        "A non-empty reminder title is required."
                    )

                raw_when = arguments.get("first_run_at")

                if not isinstance(raw_when, str) or not raw_when.strip():
                    raise ValueError(
                        "first_run_at must be an ISO 8601 timestamp "
                        "with a timezone offset."
                    )

                when_text = raw_when.strip()
                if when_text.endswith(("Z", "z")):
                    when_text = when_text[:-1] + "+00:00"

                try:
                    first_run = datetime.fromisoformat(when_text)
                except ValueError:
                    raise ValueError(
                        "first_run_at must be an ISO 8601 timestamp "
                        "with a timezone offset."
                    ) from None

                user, error = await _reminder_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                if first_run.tzinfo is None:
                    first_run = first_run.replace(
                        tzinfo=ZoneInfo(tz_name)
                    )

                first_run_utc = first_run.astimezone(timezone.utc)

                # A small grace window absorbs clock skew and the
                # time Gemini spends resolving relative phrases.
                if first_run_utc < datetime.now(
                    timezone.utc
                ) - timedelta(seconds=60):
                    return {
                        "success": False,
                        "error": (
                            "That reminder time is in the past. "
                            "Please choose a future time."
                        ),
                    }

                recurrence = arguments.get("recurrence")
                # Raises ValueError for anything outside the enum.
                reminder_service.parse_recurrence(recurrence)

                reminder = await reminder_service.create_reminder(
                    session,
                    user,
                    title=title.strip(),
                    first_run_at=first_run_utc,
                    recurrence=recurrence,
                    timezone_name=tz_name,
                )

                return {
                    "success": True,
                    "message": "Reminder created.",
                    "reminder": {
                        "id": str(reminder.id),
                        "title": reminder.title,
                        "recurrence": reminder_service.recurrence_label(
                            reminder.recurrence_rule
                        ),
                        "first_run_at": first_run_utc.isoformat(),
                        "first_run_local": _format_local_time(
                            first_run_utc,
                            tz_name,
                        ),
                    },
                }

            if tool_name == "list_reminders":
                user, error = await _reminder_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                pairs = await reminder_service.list_active_reminders(
                    session,
                    user.id,
                )

                return {
                    "success": True,
                    "count": len(pairs),
                    "reminders": [
                        _reminder_to_dict(reminder, next_at, tz_name)
                        for reminder, next_at in pairs
                    ],
                }

            if tool_name == "cancel_reminder":
                user, error = await _reminder_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                reminder_id = await _resolve_reminder_id(
                    session,
                    user.id,
                    arguments.get("reminder_id"),
                )

                if reminder_id is None:
                    return {
                        "success": False,
                        "error": (
                            "That reminder was not found among "
                            "your reminders."
                        ),
                    }

                cancelled = await reminder_service.cancel_reminder(
                    session,
                    user.id,
                    reminder_id,
                )

                if not cancelled:
                    return {
                        "success": False,
                        "error": (
                            "That reminder was not found among "
                            "your reminders."
                        ),
                    }

                return {
                    "success": True,
                    "message": "Reminder cancelled.",
                }

            if tool_name == "snooze_reminder":
                minutes = _integer(
                    arguments.get("minutes", 10),
                    "minutes",
                    minimum=1,
                    maximum=10080,
                )

                user, error = await _reminder_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                reminder_id = await _resolve_reminder_id(
                    session,
                    user.id,
                    arguments.get("reminder_id"),
                )

                if reminder_id is None:
                    return {
                        "success": False,
                        "error": (
                            "That reminder was not found among "
                            "your reminders."
                        ),
                    }

                snoozed = await reminder_service.snooze_occurrence(
                    session,
                    user.id,
                    reminder_id,
                    minutes,
                )

                if not snoozed:
                    return {
                        "success": False,
                        "error": (
                            "That reminder has no scheduled "
                            "delivery left to snooze."
                        ),
                    }

                return {
                    "success": True,
                    "message": f"Reminder snoozed by {minutes} minutes.",
                    "minutes": minutes,
                }

            if tool_name == "add_transaction":
                txn_type = arguments.get("type")
                if txn_type not in finance_service.TRANSACTION_TYPES:
                    raise ValueError(
                        "type must be 'income' or 'expense'."
                    )

                # Raises ValueError for missing/invalid amounts.
                amount = finance_service.parse_amount(
                    arguments.get("amount")
                )

                category = arguments.get("category") or "Other"
                if not isinstance(category, str):
                    raise ValueError("category must be a string.")

                note = arguments.get("note")
                if note is not None and not isinstance(note, str):
                    raise ValueError("note must be a string.")

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                occurred_at = None
                if arguments.get("occurred_at"):
                    occurred_at = _parse_tool_datetime(
                        arguments["occurred_at"],
                        tz_name,
                    )

                transaction = await finance_service.add_transaction(
                    session,
                    user.id,
                    type=txn_type,
                    amount=amount,
                    category=category,
                    note=note,
                    occurred_at=occurred_at,
                )

                return {
                    "success": True,
                    "message": "Transaction recorded.",
                    "transaction": _transaction_to_dict(
                        transaction,
                        tz_name,
                    ),
                }

            if tool_name == "get_finance_summary":
                period = arguments.get("period") or "month"
                if not isinstance(period, str):
                    raise ValueError("period must be a string.")

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)
                tz = ZoneInfo(tz_name)

                # Raises ValueError for unknown periods.
                since, until = finance_service.period_range(
                    period.strip().lower(),
                    tz,
                    datetime.now(timezone.utc),
                )

                summary = await finance_service.summarize(
                    session,
                    user.id,
                    since=since,
                    until=until,
                )

                return {
                    "success": True,
                    "period": period.strip().lower(),
                    "currency": finance_service.DEFAULT_CURRENCY,
                    **summary,
                }

            if tool_name == "list_transactions":
                period = arguments.get("period") or "month"
                if not isinstance(period, str):
                    raise ValueError("period must be a string.")

                txn_type = arguments.get("type")
                if txn_type is not None and (
                    txn_type not in finance_service.TRANSACTION_TYPES
                ):
                    raise ValueError(
                        "type must be 'income' or 'expense'."
                    )

                limit = _integer(
                    arguments.get("limit", 20),
                    "limit",
                    minimum=1,
                    maximum=50,
                )

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)
                tz = ZoneInfo(tz_name)

                since, until = finance_service.period_range(
                    period.strip().lower(),
                    tz,
                    datetime.now(timezone.utc),
                )

                category = arguments.get("category")
                normalized_category = None
                if isinstance(category, str) and category.strip():
                    if txn_type is not None:
                        normalized_category = (
                            finance_service.normalize_category(
                                txn_type,
                                category,
                            )
                        )
                    else:
                        # Without a type, prefer an exact match in
                        # either vocabulary; otherwise pass the
                        # raw text through (it will match nothing
                        # rather than the wrong "Other" bucket).
                        expense_match = (
                            finance_service.normalize_category(
                                "expense",
                                category,
                            )
                        )
                        income_match = (
                            finance_service.normalize_category(
                                "income",
                                category,
                            )
                        )
                        if expense_match.lower() == category.strip().lower():
                            normalized_category = expense_match
                        elif income_match.lower() == category.strip().lower():
                            normalized_category = income_match
                        else:
                            normalized_category = category.strip()

                transactions = await finance_service.list_transactions(
                    session,
                    user.id,
                    since=since,
                    until=until,
                    type=txn_type,
                    category=normalized_category,
                    limit=limit,
                )

                return {
                    "success": True,
                    "period": period.strip().lower(),
                    "count": len(transactions),
                    "transactions": [
                        _transaction_to_dict(txn, tz_name)
                        for txn in transactions
                    ],
                }

            if tool_name == "add_debt":
                direction = arguments.get("direction")
                if direction not in finance_service.DEBT_DIRECTIONS:
                    raise ValueError(
                        "direction must be 'borrowed' or 'lent'."
                    )

                amount = finance_service.parse_amount(
                    arguments.get("amount")
                )

                counterparty = arguments.get("counterparty")
                if not isinstance(counterparty, str) or not (
                    counterparty.strip()
                ):
                    raise ValueError(
                        "A counterparty name is required."
                    )

                kind = arguments.get("kind") or "debt"

                note = arguments.get("note")
                if note is not None and not isinstance(note, str):
                    raise ValueError("note must be a string.")

                due_date = None
                raw_due = arguments.get("due_date")
                if raw_due:
                    if not isinstance(raw_due, str):
                        raise ValueError(
                            "due_date must be a date in YYYY-MM-DD format."
                        )
                    try:
                        due_date = datetime.strptime(
                            raw_due.strip(),
                            "%Y-%m-%d",
                        ).date()
                    except ValueError:
                        raise ValueError(
                            "due_date must be a date in YYYY-MM-DD format."
                        ) from None

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                debt = await finance_service.add_debt(
                    session,
                    user.id,
                    direction=direction,
                    kind=kind,
                    counterparty=counterparty,
                    amount=amount,
                    note=note,
                    due_date=due_date,
                )

                return {
                    "success": True,
                    "message": "Debt recorded.",
                    "debt": _debt_to_dict(debt),
                }

            if tool_name == "record_debt_payment":
                amount = finance_service.parse_amount(
                    arguments.get("amount")
                )

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                debt_id = await _resolve_debt_id(
                    session,
                    user.id,
                    arguments.get("debt_id"),
                )

                if debt_id is None:
                    return {
                        "success": False,
                        "error": (
                            "That debt was not found among "
                            "your open debts."
                        ),
                    }

                # Raises ValueError for settled debts and
                # payments above the remaining balance.
                debt = await finance_service.record_debt_payment(
                    session,
                    user.id,
                    debt_id,
                    amount,
                )

                return {
                    "success": True,
                    "message": (
                        "Payment recorded. The debt is now settled."
                        if debt.status == "settled"
                        else "Payment recorded."
                    ),
                    "debt": _debt_to_dict(debt),
                }

            if tool_name == "list_debts":
                direction = arguments.get("direction")
                if direction is not None and (
                    direction not in finance_service.DEBT_DIRECTIONS
                ):
                    raise ValueError(
                        "direction must be 'borrowed' or 'lent'."
                    )

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                debts = await finance_service.list_debts(
                    session,
                    user.id,
                    direction=direction,
                )
                totals = await finance_service.debt_totals(
                    session,
                    user.id,
                )

                return {
                    "success": True,
                    "count": len(debts),
                    "debts": [_debt_to_dict(debt) for debt in debts],
                    "total_you_owe": totals["owed_by_user"],
                    "total_owed_to_you": totals["owed_to_user"],
                    "currency": finance_service.DEFAULT_CURRENCY,
                }

            if tool_name == "set_expense_reminders":
                enabled = arguments.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false.")

                times_per_day = _integer(
                    arguments.get("times_per_day", 4),
                    "times_per_day",
                    minimum=1,
                    maximum=8,
                )

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                result = await finance_service.set_expense_nudges(
                    session,
                    user,
                    enabled=enabled,
                    times_per_day=times_per_day,
                    timezone_name=tz_name,
                )

                return {
                    "success": True,
                    "message": (
                        "Expense check-in reminders are on."
                        if result["enabled"]
                        else "Expense check-in reminders are off."
                    ),
                    **result,
                }

            if tool_name == "get_daily_overview":
                day = arguments.get("day") or "yesterday"
                if day not in ("yesterday", "today"):
                    raise ValueError(
                        "day must be 'yesterday' or 'today'."
                    )

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                overview = await finance_service.build_day_overview(
                    session,
                    user.id,
                    ZoneInfo(tz_name),
                    datetime.now(timezone.utc),
                    day=day,
                )

                return {
                    "success": True,
                    "day": day,
                    "overview": overview,
                }

            if tool_name == "set_daily_overview":
                enabled = arguments.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false.")

                time_str = arguments.get("time")
                if time_str is not None and not isinstance(time_str, str):
                    raise ValueError(
                        "time must be in HH:MM format (e.g. 08:00)."
                    )

                user, error = await _finance_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                # Raises ValueError for a malformed time.
                result = await finance_service.set_daily_overview(
                    session,
                    user,
                    enabled=enabled,
                    time_str=time_str,
                    timezone_name=tz_name,
                )

                return {
                    "success": True,
                    "message": (
                        "Morning overview is on."
                        if result["enabled"]
                        else "Morning overview is off."
                    ),
                    **result,
                }

            if tool_name == "create_task":
                title = arguments.get("title")

                if not isinstance(title, str) or not title.strip():
                    raise ValueError(
                        "A non-empty task title is required."
                    )

                notes = arguments.get("notes")
                if notes is not None and not isinstance(notes, str):
                    raise ValueError("notes must be a string.")

                user, error = await _task_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                due_at = None
                if arguments.get("due_at"):
                    due_at = _parse_tool_datetime(
                        arguments["due_at"],
                        tz_name,
                    )

                task = await task_service.create_task(
                    session,
                    user.id,
                    title=title,
                    notes=notes,
                    due_at=due_at,
                )

                return {
                    "success": True,
                    "message": "Task added to your list.",
                    "task": _task_to_dict(task, tz_name),
                }

            if tool_name == "list_tasks":
                status = arguments.get("status") or "open"
                if status not in (*task_service.TASK_STATUSES, "all"):
                    raise ValueError(
                        "status must be 'open', 'done', or 'all'."
                    )

                user, error = await _task_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                tasks = await task_service.list_tasks(
                    session,
                    user.id,
                    status=status,
                )

                return {
                    "success": True,
                    "count": len(tasks),
                    "tasks": [
                        _task_to_dict(task, tz_name) for task in tasks
                    ],
                }

            if tool_name == "complete_task":
                user, error = await _task_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                # Raises ValueError when the reference is
                # unknown, ambiguous, or already done.
                task = await task_service.complete_task(
                    session,
                    user.id,
                    arguments.get("task_id"),
                )

                return {
                    "success": True,
                    "message": "Task marked as done.",
                    "task": _task_to_dict(task, tz_name),
                }

            if tool_name == "delete_task":
                user, error = await _task_actor(
                    session,
                    telegram_user_id,
                )
                if error is not None:
                    return error

                tz_name = await _user_timezone_name(session, user.id)

                # Raises ValueError when the reference is
                # unknown, ambiguous, or already done.
                task = await task_service.delete_task(
                    session,
                    user.id,
                    arguments.get("task_id"),
                )

                return {
                    "success": True,
                    "message": "Task deleted.",
                    "task": _task_to_dict(task, tz_name),
                }

            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
            }

    except PermissionError as exc:
        logger.warning(
            "AI tool permission denied: tool=%s actor=%s",
            tool_name,
            telegram_user_id,
        )
        return {
            "success": False,
            "error": str(exc),
        }

    except ValueError as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    except Exception:
        logger.exception(
            "AI tool execution failed: tool=%s actor=%s",
            tool_name,
            telegram_user_id,
        )
        return {
            "success": False,
            "error": "The backend could not complete this operation.",
        }