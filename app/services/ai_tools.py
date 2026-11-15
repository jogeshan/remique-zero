import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import AdminAuditLog, TelegramAccount, User
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