import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.database import get_session_factory
from app.models import InviteCode, User
from app.services.invite_service import (
    create_invite,
    hash_invite_code,
    revoke_invite,
)
from app.services.auth_service import (
    register_telegram_user,
    InvalidRegistrationInvite,
    TelegramAccountAlreadyLinked,
)


@pytest.mark.asyncio
async def test_invite_and_registration_flow():
    session_factory = get_session_factory()

    created_user_ids = []
    created_invite_hashes = []

    telegram_id = secrets.randbelow(10**12) + 10**12
    chat_id = telegram_id

    try:
        async with session_factory() as session:

            # Test 1: Valid invite activates a new user.
            invite = await create_invite(session, max_uses=1)
            created_invite_hashes.append(hash_invite_code(invite.code))

            registered = await register_telegram_user(
                session,
                invite_code=invite.code,
                telegram_user_id=telegram_id,
                chat_id=chat_id,
                username="remique_test",
                display_name="Remique Test",
            )

            created_user_ids.append(registered.user_id)

            assert registered.status == "active"
            assert registered.role == "user"
            print("PASS: Valid invite activates a user.")

            # Test 2: Exhausted invite cannot be reused.
            with pytest.raises(InvalidRegistrationInvite):
                await register_telegram_user(
                    session,
                    invite_code=invite.code,
                    telegram_user_id=telegram_id + 1,
                    chat_id=chat_id + 1,
                )

            print("PASS: Exhausted invite rejected.")

            # Test 3: Revoked invite is rejected.
            revoked = await create_invite(session)
            created_invite_hashes.append(hash_invite_code(revoked.code))

            assert await revoke_invite(session, revoked.invite_id)

            with pytest.raises(InvalidRegistrationInvite):
                await register_telegram_user(
                    session,
                    invite_code=revoked.code,
                    telegram_user_id=telegram_id + 2,
                    chat_id=chat_id + 2,
                )

            print("PASS: Revoked invite rejected.")

            # Test 4: Expired invite is rejected.
            expired_code = "rz_expired_" + secrets.token_urlsafe(12)
            expired_hash = hash_invite_code(expired_code)
            created_invite_hashes.append(expired_hash)

            session.add(
                InviteCode(
                    code_hash=expired_hash,
                    max_uses=1,
                    use_count=0,
                    expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
                )
            )
            await session.commit()

            with pytest.raises(InvalidRegistrationInvite):
                await register_telegram_user(
                    session,
                    invite_code=expired_code,
                    telegram_user_id=telegram_id + 3,
                    chat_id=chat_id + 3,
                )

            print("PASS: Expired invite rejected.")

            # Test 5: Duplicate Telegram account rejected.
            second_invite = await create_invite(session)
            created_invite_hashes.append(
                hash_invite_code(second_invite.code)
            )

            with pytest.raises(TelegramAccountAlreadyLinked):
                await register_telegram_user(
                    session,
                    invite_code=second_invite.code,
                    telegram_user_id=telegram_id,
                    chat_id=chat_id,
                )

            print("PASS: Duplicate Telegram account rejected.")

            # Confirm failed registration did not consume its invite.
            remaining = await session.scalar(
                select(InviteCode).where(
                    InviteCode.code_hash == hash_invite_code(
                        second_invite.code
                    )
                )
            )

            assert remaining is not None
            assert remaining.use_count == 0

            print("PASS: Failed registration did not consume invite.")

    finally:
        # Remove only records created by this test.
        async with session_factory() as cleanup:
            if created_user_ids:
                await cleanup.execute(
                    delete(User).where(User.id.in_(created_user_ids))
                )

            if created_invite_hashes:
                await cleanup.execute(
                    delete(InviteCode).where(
                        InviteCode.code_hash.in_(created_invite_hashes)
                    )
                )

            await cleanup.commit()

    print("\nALL AUTHENTICATION INTEGRATION TESTS PASSED.")
