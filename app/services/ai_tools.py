import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.models import AdminAuditLog, TelegramAccount, User, UserSetting
from app.services import memory_service, reminder_service
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
            "every user, for their own memory only."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum memories to return. Defaults to 20.",
                    "minimum": 1,
                    "maximum": 50,
                }
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