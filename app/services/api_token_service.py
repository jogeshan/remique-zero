import hashlib
import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiToken, User

logger = logging.getLogger(__name__)

# Raw tokens carry a recognizable prefix so a leaked one is easy to
# spot in logs and screenshots. Only the SHA-256 hash is stored.
TOKEN_PREFIX = "rz_"
DEFAULT_TOKEN_LABEL = "dashboard"
MAX_LABEL_LENGTH = 100


def generate_raw_token() -> str:
    """Return a fresh raw API token (shown to the user once)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest stored for a raw token."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


async def create_token(
    session: AsyncSession,
    user_id: UUID,
    *,
    label: str = DEFAULT_TOKEN_LABEL,
) -> tuple[ApiToken, str]:
    """
    Create a new API token for a user, rotating out the old ones.

    Every still-active token of the user is revoked first, so at
    most one dashboard token is valid at a time. Returns the stored
    row and the raw token; the raw token exists only in this return
    value and must be shown to the user immediately — it cannot be
    recovered later.
    """
    now = datetime.now(timezone.utc)

    existing = await session.scalars(
        select(ApiToken).where(
            ApiToken.user_id == user_id,
            ApiToken.revoked_at.is_(None),
        )
    )
    for old in existing:
        old.revoked_at = now

    raw_token = generate_raw_token()
    token = ApiToken(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        label=(label or DEFAULT_TOKEN_LABEL).strip()[:MAX_LABEL_LENGTH]
        or DEFAULT_TOKEN_LABEL,
        created_at=now,
    )
    session.add(token)
    await session.commit()
    return token, raw_token


async def authenticate_token(
    session: AsyncSession,
    raw_token: str,
) -> User | None:
    """
    Resolve the active user for a raw bearer token, or None.

    Rejects unknown and revoked tokens, and tokens whose owner is
    no longer an active account. On success the token's
    last_used_at is updated (best-effort: a failure there never
    blocks authentication).
    """
    if not raw_token or not isinstance(raw_token, str):
        return None

    token = await session.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(raw_token),
            ApiToken.revoked_at.is_(None),
        )
    )

    if token is None:
        return None

    user = await session.scalar(
        select(User).where(
            User.id == token.user_id,
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )

    if user is None:
        return None

    token.last_used_at = datetime.now(timezone.utc)
    try:
        await session.commit()
    except Exception:
        logger.warning(
            "Failed to update API token last_used_at.",
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:
            pass

    return user
