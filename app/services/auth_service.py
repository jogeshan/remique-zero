import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    AdminAuditLog,
    InviteCode,
    TelegramAccount,
    User,
    UserSetting,
)
from app.services.invite_service import hash_invite_code


class RegistrationError(Exception):
    pass


class InvalidRegistrationInvite(RegistrationError):
    pass


class TelegramAccountAlreadyLinked(RegistrationError):
    pass


class InvalidUserTimezone(RegistrationError):
    pass


@dataclass(frozen=True)
class RegistrationResult:
    user_id: uuid.UUID
    telegram_user_id: int
    display_name: str | None
    status: str
    role: str


async def register_telegram_user(
    session: AsyncSession,
    *,
    invite_code: str | None,
    telegram_user_id: int,
    chat_id: int,
    username: str | None = None,
    display_name: str | None = None,
    timezone_name: str = "Asia/Dhaka",
) -> RegistrationResult:

    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidUserTimezone("Invalid timezone.") from exc

    if telegram_user_id <= 0 or chat_id == 0:
        raise RegistrationError("Invalid Telegram account information.")

    clean_name = display_name.strip()[:120] if display_name else None
    clean_username = username.strip()[:100] if username else None

    if not clean_name:
        clean_name = None

    if not clean_username:
        clean_username = None

    bootstrap_id = settings.bootstrap_admin_telegram_id.strip()
    bootstrap_candidate = (
        bootstrap_id.isdigit()
        and bootstrap_id == str(telegram_user_id)
    )

    try:
        async with session.begin():

            existing = await session.scalar(
                select(TelegramAccount).where(
                    TelegramAccount.telegram_user_id == telegram_user_id
                )
            )

            if existing is not None:
                raise TelegramAccountAlreadyLinked(
                    "This Telegram account is already registered."
                )

            is_bootstrap = False

            if bootstrap_candidate:
                # Serialize bootstrap attempts across database connections.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(7834021901)")
                )

                existing_super_admin = await session.scalar(
                    select(User.id)
                    .where(User.role == "super_admin")
                    .limit(1)
                )

                is_bootstrap = existing_super_admin is None

            if not is_bootstrap:
                if not invite_code:
                    raise InvalidRegistrationInvite(
                        "A valid invitation is required."
                    )

                if len(invite_code) > 256:
                    raise InvalidRegistrationInvite(
                        "Invalid invitation."
                    )

                code_hash = hash_invite_code(invite_code)

                redemption = await session.execute(
                    update(InviteCode)
                    .where(
                        InviteCode.code_hash == code_hash,
                        InviteCode.revoked_at.is_(None),
                        InviteCode.use_count < InviteCode.max_uses,
                        (
                            (InviteCode.expires_at.is_(None))
                            | (InviteCode.expires_at > func.now())
                        ),
                    )
                    .values(use_count=InviteCode.use_count + 1)
                    .returning(InviteCode.id)
                )

                invite_id = redemption.scalar_one_or_none()

                if invite_id is None:
                    raise InvalidRegistrationInvite(
                        "This invitation is invalid or unavailable."
                    )

            assigned_role = "super_admin" if is_bootstrap else "user"

            user = User(
                display_name=clean_name,
                status="active",
                role=assigned_role,
            )

            session.add(user)
            await session.flush()

            telegram_account = TelegramAccount(
                user_id=user.id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                username=clean_username,
            )

            user_settings = UserSetting(
                user_id=user.id,
                timezone=timezone_name,
            )

            session.add_all([telegram_account, user_settings])

            if is_bootstrap:
                session.add(
                    AdminAuditLog(
                        actor_user_id=user.id,
                        target_user_id=user.id,
                        action="admin.bootstrap_completed",
                        metadata_json={
                            "method": "telegram_id_bootstrap",
                        },
                    )
                )

            await session.flush()

            result = RegistrationResult(
                user_id=user.id,
                telegram_user_id=telegram_user_id,
                display_name=user.display_name,
                status=user.status,
                role=user.role,
            )

        return result

    except IntegrityError as exc:
        raise TelegramAccountAlreadyLinked(
            "This Telegram account is already registered."
        ) from exc
