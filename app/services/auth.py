"""Invite-only account registration and Telegram identity linking."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AdminAuditLog, InviteCode, TelegramAccount, User, UserSetting


class AuthError(ValueError):
    """A safe, user-displayable authentication or invite error."""


@dataclass(frozen=True)
class IssuedInvite:
    code: str
    invite_id: uuid.UUID
    expires_at: datetime | None
    max_uses: int


def generate_invite_code() -> str:
    """Return a high-entropy code; only its SHA-256 digest is persisted."""
    return secrets.token_urlsafe(32)


def hash_invite_code(code: str) -> str:
    """Hash invite codes before lookup/storage; codes contain 256 random bits."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require_admin(session: AsyncSession, actor_id: uuid.UUID) -> User:
    actor = await session.get(User, actor_id)
    if actor is None or actor.deleted_at is not None or actor.status != "active":
        raise AuthError("An active administrator is required.")
    if actor.role not in {"admin", "super_admin"}:
        raise AuthError("An active administrator is required.")
    return actor


async def create_invite(
    session: AsyncSession,
    actor_id: uuid.UUID,
    *,
    max_uses: int = 1,
    expires_in: timedelta | None = None,
) -> IssuedInvite:
    if max_uses < 1:
        raise AuthError("Invite use limit must be at least one.")
    if expires_in is not None and expires_in <= timedelta(0):
        raise AuthError("Invite expiration must be in the future.")
    await _require_admin(session, actor_id)
    code = generate_invite_code()
    expires_at = _now() + expires_in if expires_in is not None else None
    invite = InviteCode(
        id=uuid.uuid4(), code_hash=hash_invite_code(code), created_by=actor_id,
        max_uses=max_uses, use_count=0, expires_at=expires_at,
    )
    session.add(invite)
    session.add(AdminAuditLog(actor_user_id=actor_id, action="invite.created", metadata_json={"invite_id": str(invite.id), "max_uses": max_uses}))
    await session.flush()
    return IssuedInvite(code, invite.id, expires_at, max_uses)


async def revoke_invite(session: AsyncSession, actor_id: uuid.UUID, invite_id: uuid.UUID) -> None:
    await _require_admin(session, actor_id)
    invite = await session.get(InviteCode, invite_id)
    if invite is None:
        raise AuthError("Invite not found.")
    if invite.revoked_at is None:
        invite.revoked_at = _now()
        session.add(AdminAuditLog(actor_user_id=actor_id, action="invite.revoked", metadata_json={"invite_id": str(invite_id)}))
    await session.flush()


async def redeem_invite(
    session: AsyncSession,
    code: str,
    *,
    telegram_user_id: int,
    chat_id: int,
    username: str | None = None,
    display_name: str | None = None,
) -> User:
    """Atomically consume an invite and activate a regular Telegram account."""
    if not code or len(code) > 256:
        raise AuthError("Invite is invalid, expired, revoked, or fully used.")
    if telegram_user_id <= 0:
        raise AuthError("Telegram account is invalid.")
    existing = await session.scalar(select(TelegramAccount).where(TelegramAccount.telegram_user_id == telegram_user_id))
    if existing is not None:
        raise AuthError("This Telegram account is already linked.")

    now = _now()
    claimed = await session.execute(
        update(InviteCode)
        .where(
            InviteCode.code_hash == hash_invite_code(code),
            InviteCode.revoked_at.is_(None),
            (InviteCode.expires_at.is_(None) | (InviteCode.expires_at > now)),
            InviteCode.use_count < InviteCode.max_uses,
        )
        .values(use_count=InviteCode.use_count + 1)
        .returning(InviteCode.id)
    )
    invite_id = claimed.scalar_one_or_none()
    if invite_id is None:
        raise AuthError("Invite is invalid, expired, revoked, or fully used.")

    user = User(display_name=(display_name or username or "")[:120] or None, status="active", role="user")
    session.add(user)
    await session.flush()
    session.add(TelegramAccount(
        user_id=user.id, telegram_user_id=telegram_user_id, chat_id=chat_id,
        username=username[:100] if username else None,
    ))
    session.add(UserSetting(user_id=user.id, timezone=settings.default_timezone))
    session.add(AdminAuditLog(target_user_id=user.id, action="account.registered", metadata_json={"invite_id": str(invite_id)}))
    await session.flush()
    return user


async def link_telegram_account(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    telegram_user_id: int,
    chat_id: int,
    username: str | None = None,
) -> TelegramAccount:
    """Link an unclaimed Telegram identity without changing user privileges."""
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None or user.status != "active":
        raise AuthError("Only an active account can link Telegram.")
    if telegram_user_id <= 0:
        raise AuthError("Telegram account is invalid.")
    if await session.scalar(select(TelegramAccount).where(TelegramAccount.telegram_user_id == telegram_user_id)):
        raise AuthError("This Telegram account is already linked.")
    account = TelegramAccount(
        user_id=user.id, telegram_user_id=telegram_user_id, chat_id=chat_id,
        username=username[:100] if username else None,
    )
    session.add(account)
    await session.flush()
    return account
