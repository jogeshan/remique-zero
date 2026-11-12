from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAuditLog, TelegramAccount, User
from app.services.admin_service import get_admin


async def _find_target(session, telegram_user_id):
    result = await session.execute(
        select(User, TelegramAccount)
        .join(TelegramAccount, TelegramAccount.user_id == User.id)
        .where(
            TelegramAccount.telegram_user_id == telegram_user_id,
            User.deleted_at.is_(None),
        )
    )
    return result.one_or_none()


async def set_user_status(
    session: AsyncSession,
    *,
    actor_telegram_id: int,
    target_telegram_id: int,
    new_status: str,
) -> bool:

    if new_status not in ("active", "disabled"):
        raise ValueError("Unsupported user status.")

    actor = await get_admin(session, actor_telegram_id)

    if actor is None:
        raise PermissionError("Administrator access required.")

    if actor_telegram_id == target_telegram_id:
        raise PermissionError("You cannot change your own account status.")

    target_data = await _find_target(session, target_telegram_id)

    if target_data is None:
        await session.rollback()
        return False

    target, telegram_account = target_data

    if target.role == "super_admin":
        raise PermissionError("Super-admin accounts are protected.")

    if actor.role == "admin" and target.role != "user":
        raise PermissionError("Admins can only manage regular users.")

    if target.status == new_status:
        await session.rollback()
        return False

    target.status = new_status

    session.add(
        AdminAuditLog(
            actor_user_id=actor.id,
            target_user_id=target.id,
            action=f"user.{new_status}",
            metadata_json={
                "telegram_user_id": target_telegram_id,
                "previous_status": "disabled" if new_status == "active" else "active",
                "new_status": new_status,
            },
        )
    )

    await session.commit()
    return True


async def change_user_role(
    session: AsyncSession,
    *,
    actor_telegram_id: int,
    target_telegram_id: int,
    new_role: str,
) -> bool:

    if new_role not in ("user", "admin"):
        raise ValueError("Unsupported role.")

    actor = await get_admin(session, actor_telegram_id)

    if actor is None or actor.role != "super_admin":
        raise PermissionError("Super-admin access required.")

    if actor_telegram_id == target_telegram_id:
        raise PermissionError("You cannot change your own role.")

    target_data = await _find_target(session, target_telegram_id)

    if target_data is None:
        await session.rollback()
        return False

    target, telegram_account = target_data

    if target.role == "super_admin":
        raise PermissionError("Super-admin accounts are protected.")

    if target.role == new_role:
        await session.rollback()
        return False

    previous_role = target.role
    target.role = new_role

    session.add(
        AdminAuditLog(
            actor_user_id=actor.id,
            target_user_id=target.id,
            action=f"user.role_changed",
            metadata_json={
                "telegram_user_id": target_telegram_id,
                "previous_role": previous_role,
                "new_role": new_role,
            },
        )
    )

    await session.commit()
    return True
