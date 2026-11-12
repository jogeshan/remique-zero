import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InviteCode


class InvalidInviteError(Exception):
    """The invite is invalid, expired, revoked, or exhausted."""


@dataclass(frozen=True)
class CreatedInvite:
    invite_id: uuid.UUID
    code: str
    max_uses: int
    expires_at: datetime | None


def hash_invite_code(code: str) -> str:
    normalized = code.strip()

    if not normalized:
        raise ValueError("Invite code cannot be empty.")

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def create_invite(
    session: AsyncSession,
    *,
    created_by: uuid.UUID | None = None,
    max_uses: int = 1,
    expires_at: datetime | None = None,
    commit: bool = True,
) -> CreatedInvite:
    if max_uses < 1:
        raise ValueError("max_uses must be at least 1.")

    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware.")

        if expires_at <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future.")

    raw_code = "rz_" + secrets.token_urlsafe(32)

    invite = InviteCode(
        code_hash=hash_invite_code(raw_code),
        created_by=created_by,
        max_uses=max_uses,
        use_count=0,
        expires_at=expires_at,
    )

    session.add(invite)
    await session.flush()
    await session.refresh(invite)
    if commit:
        await session.commit()

    return CreatedInvite(
        invite_id=invite.id,
        code=raw_code,
        max_uses=invite.max_uses,
        expires_at=invite.expires_at,
    )


async def revoke_invite(
    session: AsyncSession,
    invite_id: uuid.UUID,
    *,
    commit: bool = True,
) -> bool:
    result = await session.execute(
        update(InviteCode)
        .where(
            InviteCode.id == invite_id,
            InviteCode.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )

    if commit:
        await session.commit()

    return result.rowcount == 1
