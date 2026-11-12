from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAuditLog, InviteCode, TelegramAccount, User
from app.services.invite_service import (
    create_invite,
    hash_invite_code,
    revoke_invite,
)


async def get_admin(
    session: AsyncSession,
    telegram_user_id: int,
) -> User | None:
    user = await session.scalar(
        select(User)
        .join(TelegramAccount, TelegramAccount.user_id == User.id)
        .where(
            TelegramAccount.telegram_user_id == telegram_user_id,
            User.status == "active",
            User.deleted_at.is_(None),
            User.role.in_(["admin", "super_admin"]),
        )
    )
    return user


async def create_admin_invite(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    max_uses: int = 1,
    expires_in_days: int | None = None,
):
    admin = await get_admin(session, telegram_user_id)

    if admin is None:
        raise PermissionError("Administrator access required.")

    expires_at = None
    if expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=expires_in_days
        )

    invite = await create_invite(
        session,
        created_by=admin.id,
        max_uses=max_uses,
        expires_at=expires_at,
        commit=False,
    )

    session.add(
        AdminAuditLog(
            actor_user_id=admin.id,
            action="invite.created",
            metadata_json={
                "invite_id": str(invite.invite_id),
                "max_uses": max_uses,
                "expires_at": (
                    expires_at.isoformat() if expires_at else None
                ),
            },
        )
    )

    await session.commit()
    return invite


async def revoke_admin_invite(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    code: str,
) -> bool:
    admin = await get_admin(session, telegram_user_id)

    if admin is None:
        raise PermissionError("Administrator access required.")

    code_hash = hash_invite_code(code)

    invite = await session.scalar(
        select(InviteCode).where(InviteCode.code_hash == code_hash)
    )

    if invite is None:
        await session.rollback()
        return False

    revoked = await revoke_invite(
        session,
        invite.id,
        commit=False,
    )

    if revoked:
        session.add(
            AdminAuditLog(
                actor_user_id=admin.id,
                action="invite.revoked",
                metadata_json={"invite_id": str(invite.id)},
            )
        )

    await session.commit()
    return revoked


async def get_user_statistics(
    session: AsyncSession,
    telegram_user_id: int,
) -> dict:
    admin = await get_admin(session, telegram_user_id)

    if admin is None:
        raise PermissionError("Administrator access required.")

    total = await session.scalar(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None))
    )

    active = await session.scalar(
        select(func.count()).select_from(User)
        .where(User.status == "active", User.deleted_at.is_(None))
    )

    disabled = await session.scalar(
        select(func.count()).select_from(User)
        .where(User.status == "disabled", User.deleted_at.is_(None))
    )

    admins = await session.scalar(
        select(func.count()).select_from(User)
        .where(
            User.role.in_(["admin", "super_admin"]),
            User.deleted_at.is_(None),
        )
    )

    return {
        "total": total or 0,
        "active": active or 0,
        "disabled": disabled or 0,
        "admins": admins or 0,
    }
