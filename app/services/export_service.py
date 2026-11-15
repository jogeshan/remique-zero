"""
Personal data export (Phase 12).

Builds one JSON document with everything the bot stores about a
single user: profile, settings, memories, reminders,
transactions, debts with their payment ledger, and tasks. The
builder only ever queries with the given user's id, so an export
cannot contain another user's data by construction.

Caps keep the document (and the Telegram file upload) sane:
transactions are the most recent MAX_TRANSACTIONS, memories and
tasks are capped at MAX_EXPORT_ROWS. Reminders cover active
reminders — the same view as /reminders — via the reminder
service.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    DebtPayment,
    TelegramAccount,
    User,
    UserSetting,
)
from app.services import (
    finance_service,
    memory_service,
    reminder_service,
    task_service,
)

logger = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION = 1
MAX_TRANSACTIONS = 1000
MAX_EXPORT_ROWS = 1000


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _amount(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


async def build_user_export(
    session: AsyncSession,
    user: User,
) -> dict:
    """Assemble the full export document for one user."""
    user_id: UUID = user.id
    now = datetime.now(timezone.utc)

    account = await session.scalar(
        select(TelegramAccount).where(
            TelegramAccount.user_id == user_id
        )
    )
    setting = await session.scalar(
        select(UserSetting).where(UserSetting.user_id == user_id)
    )

    memories = await memory_service.list_memories(
        session, user_id, limit=MAX_EXPORT_ROWS
    )
    reminder_pairs = await reminder_service.list_active_reminders(
        session, user_id
    )
    transactions = await finance_service.list_transactions(
        session,
        user_id,
        since=datetime(2000, 1, 1, tzinfo=timezone.utc),
        until=now + timedelta(days=1),
        limit=MAX_TRANSACTIONS,
    )

    debts = [
        *await finance_service.list_debts(
            session, user_id, status="open"
        ),
        *await finance_service.list_debts(
            session, user_id, status="settled"
        ),
    ]
    payments = list(
        await session.scalars(
            select(DebtPayment)
            .where(DebtPayment.user_id == user_id)
            .order_by(
                DebtPayment.paid_at.asc(),
                DebtPayment.id.asc(),
            )
        )
    )
    payments_by_debt: dict[str, list[dict]] = {}
    for payment in payments:
        payments_by_debt.setdefault(str(payment.debt_id), []).append(
            {
                "id": str(payment.id),
                "amount": _amount(payment.amount),
                "paid_at": _iso(payment.paid_at),
            }
        )

    tasks = await task_service.list_tasks(
        session, user_id, status="all"
    )

    document = {
        "export": {
            "format_version": EXPORT_FORMAT_VERSION,
            "application": settings.app_name,
            "exported_at": _iso(now),
            "caps": {
                "transactions": MAX_TRANSACTIONS,
                "memories": MAX_EXPORT_ROWS,
                "tasks": MAX_EXPORT_ROWS,
            },
            "notes": (
                "Transactions are capped at the most recent "
                f"{MAX_TRANSACTIONS}; memories and tasks at "
                f"{MAX_EXPORT_ROWS} each. Reminders list active "
                "reminders only."
            ),
            "counts": {
                "memories": len(memories),
                "reminders": len(reminder_pairs),
                "transactions": len(transactions),
                "debts": len(debts),
                "debt_payments": len(payments),
                "tasks": min(len(tasks), MAX_EXPORT_ROWS),
            },
        },
        "profile": {
            "user_id": str(user.id),
            "display_name": user.display_name,
            "role": user.role,
            "status": user.status,
            "member_since": _iso(user.created_at),
            "telegram": (
                {
                    "telegram_id": account.telegram_user_id,
                    "username": account.username,
                    "linked_at": _iso(account.linked_at),
                }
                if account is not None
                else None
            ),
        },
        "settings": (
            {
                "timezone": setting.timezone,
                "language": setting.language,
                "date_format": setting.date_format,
                "default_snooze_minutes": (
                    setting.default_snooze_minutes
                ),
                "quiet_hours_start": setting.quiet_hours_start,
                "quiet_hours_end": setting.quiet_hours_end,
            }
            if setting is not None
            else None
        ),
        "memories": [
            {
                "id": str(memory.id),
                "content": memory.content,
                "category": memory.category,
                "created_at": _iso(memory.created_at),
            }
            for memory in memories
        ],
        "reminders": [
            {
                "id": str(reminder.id),
                "title": reminder.title,
                "description": reminder.description,
                "timezone": reminder.timezone,
                "recurrence": reminder_service.recurrence_label(
                    reminder.recurrence_rule
                ),
                "status": reminder.status,
                "next_at": _iso(next_at),
                "created_at": _iso(reminder.created_at),
            }
            for reminder, next_at in reminder_pairs
        ],
        "transactions": [
            {
                "id": str(row.id),
                "type": row.type,
                "amount": _amount(row.amount),
                "currency": row.currency,
                "category": row.category,
                "note": row.note,
                "occurred_at": _iso(row.occurred_at),
            }
            for row in transactions
        ],
        "debts": [
            {
                "id": str(debt.id),
                "direction": debt.direction,
                "kind": debt.kind,
                "counterparty": debt.counterparty,
                "principal": _amount(debt.principal),
                "remaining": _amount(debt.remaining),
                "status": debt.status,
                "note": debt.note,
                "due_date": _iso(debt.due_date),
                "created_at": _iso(debt.created_at),
                "payments": payments_by_debt.get(str(debt.id), []),
            }
            for debt in debts
        ],
        "tasks": [
            {
                "id": str(task.id),
                "title": task.title,
                "notes": task.notes,
                "status": task.status,
                "due_at": _iso(task.due_at),
                "completed_at": _iso(task.completed_at),
                "created_at": _iso(task.created_at),
            }
            for task in tasks[:MAX_EXPORT_ROWS]
        ],
    }

    logger.info(
        "Data export built: user_id=%s counts=%s",
        user.id,
        document["export"]["counts"],
    )

    return document
