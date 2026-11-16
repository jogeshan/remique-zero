import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import (
    DeliveryAttempt,
    DeliveryJob,
    Reminder,
    ReminderOccurrence,
    TelegramAccount,
)

logger = logging.getLogger(__name__)

# --- next-due cache -------------------------------------------------
#
# The delivery loop used to query PostgreSQL every 10 seconds,
# which kept the Neon compute awake around the clock and burned
# free-tier compute hours while nothing was due. Instead, the
# loop remembers the earliest pending job time: one cheap
# min(run_at) query after each pass, refreshed early whenever
# code in this process schedules a job (note_next_due). A
# staleness cap in the loop bounds the damage if a hint is ever
# missed (e.g. a job written by another process).

_next_due_hint: datetime | None = None


def note_next_due(run_at: datetime | None) -> None:
    """Record a newly scheduled job time (earlier wins)."""
    global _next_due_hint

    if run_at is None:
        return

    if _next_due_hint is None or run_at < _next_due_hint:
        _next_due_hint = run_at


def get_next_due_hint() -> datetime | None:
    return _next_due_hint


def clear_next_due_hint() -> None:
    global _next_due_hint
    _next_due_hint = None


async def get_next_due_at(session_factory) -> datetime | None:
    """Earliest run_at among pending delivery jobs, or None."""
    from sqlalchemy import func

    async with session_factory() as session:
        return await session.scalar(
            select(func.min(DeliveryJob.run_at)).where(
                DeliveryJob.status == "pending"
            )
        )


def should_run_delivery_pass(
    now: datetime,
    earliest_due: datetime | None,
    last_pass_at: datetime | None,
    *,
    quiet_staleness_seconds: int = 1200,
    active_window_seconds: int = 900,
    active_staleness_seconds: int = 60,
) -> bool:
    """
    Decide whether the delivery loop should hit the database.

    Run a pass on startup, when the earliest known job is due,
    or when the last pass is stale. "Stale" is deliberately
    generous while the bot is quiet — 20 minutes by default —
    because every in-process scheduling path reports its job
    time through note_next_due, and Neon needs ~5 idle minutes
    to suspend its compute to zero; a 60-second safety poll
    would keep it billed around the clock for nothing. Near a
    known due time (inside the active window) the tight 60s
    staleness applies instead, so imminent deliveries stay
    precise even if a hint was somehow missed.
    """
    if last_pass_at is None:
        return True

    if earliest_due is not None and earliest_due <= now:
        return True

    age = (now - last_pass_at).total_seconds()

    if (
        earliest_due is not None
        and (earliest_due - now).total_seconds() <= active_window_seconds
    ):
        return age >= active_staleness_seconds

    return age >= quiet_staleness_seconds

# How long a claimed job stays locked before another worker may
# reclaim it (crash recovery for a worker that died mid-delivery).
LOCK_DURATION = timedelta(minutes=5)

# Retry backoff unit: after attempt N fails, the job is retried
# N * RETRY_BACKOFF_UNIT later, until max_attempts is reached.
RETRY_BACKOFF_UNIT = timedelta(minutes=5)

# Expense check-in nudges are skipped when the user has already
# logged a transaction within this window — nagging someone who
# just tracked their spending would only annoy them.
RECENT_ACTIVITY_WINDOW = timedelta(hours=3)


def retry_delay(attempts: int) -> timedelta:
    """Backoff before the next try after `attempts` failed tries."""
    return RETRY_BACKOFF_UNIT * max(attempts, 1)


def should_give_up(attempts: int, max_attempts: int) -> bool:
    """True when a job has used all of its delivery attempts."""
    return attempts >= max_attempts


# Job types whose delivered message carries the reminder action
# buttons (Done / Snooze). "reminder_snooze" is the one-off extra
# occurrence created when a delivered reminder is snoozed; its
# message gets buttons too, but delivering it must not spawn the
# next occurrence of the series.
BUTTON_JOB_TYPES = ("reminder", "reminder_snooze")


def reminder_keyboard(occurrence_id) -> InlineKeyboardMarkup:
    """
    Build the action buttons for a delivered reminder message.

    The callback data is `rem:{action}:{occurrence_id}`; with a
    36-character UUID the longest value ("rem:snooze1440:…")
    stays well inside Telegram's 64-byte callback limit.
    """
    occurrence_key = str(occurrence_id)

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Done",
                    callback_data=f"rem:done:{occurrence_key}",
                ),
                InlineKeyboardButton(
                    "⏰ Snooze 1h",
                    callback_data=f"rem:snooze60:{occurrence_key}",
                ),
                InlineKeyboardButton(
                    "🗓 Snooze tomorrow",
                    callback_data=f"rem:snooze1440:{occurrence_key}",
                ),
            ]
        ]
    )


async def _claim_due_jobs(
    session,
    *,
    now: datetime,
    worker_id: str,
    batch: int,
) -> list:
    """
    Claim due jobs for this worker and return their IDs.

    A job is due when it is pending and its run_at has passed, or
    when it is stuck in processing with an expired lock (a previous
    worker crashed while holding it). Claiming sets the lock and
    counts the attempt up front, so a crash after claiming still
    consumes an attempt and the job is reclaimed after the lock
    expires instead of being lost. SELECT ... FOR UPDATE SKIP
    LOCKED keeps concurrent workers from claiming the same job.

    This is also the Render-sleep recovery mechanism: there is no
    separate missed-job detector, because anything overdue is
    simply due on the first pass after the service wakes.
    """
    result = await session.execute(
        select(DeliveryJob)
        .where(
            or_(
                (DeliveryJob.status == "pending")
                & (DeliveryJob.run_at <= now),
                (DeliveryJob.status == "processing")
                & (DeliveryJob.locked_until.is_not(None))
                & (DeliveryJob.locked_until <= now),
            )
        )
        .order_by(DeliveryJob.run_at.asc())
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    jobs = list(result.scalars())

    for job in jobs:
        job.status = "processing"
        job.locked_until = now + LOCK_DURATION
        job.locked_by = worker_id
        job.attempts += 1

    await session.commit()

    return [job.id for job in jobs]


def _schedule_next_occurrence(
    session,
    reminder,
    delivered,
    *,
    job_type: str = "reminder",
) -> None:
    """
    Create the next occurrence (and its delivery job) for a
    recurring reminder after one occurrence was delivered.

    The schedule is anchored on the delivered occurrence's own
    scheduled time — not on "now" — so a reminder delivered late
    after downtime does not drift, and the unique
    (reminder_id, scheduled_for) constraint plus the idempotency
    key keep duplicates out. Recurrence math runs in the
    reminder's local timezone. The follow-up job keeps the
    delivered job's type, so special deliveries (expense nudges,
    the daily overview) stay special on every recurrence.
    """
    from app.services.reminder_service import compute_next_occurrence

    tz = ZoneInfo(reminder.timezone)
    anchor_local = delivered.scheduled_for.astimezone(tz)

    next_local = compute_next_occurrence(
        anchor_local,
        reminder.recurrence_rule,
        anchor_local,
    )

    if next_local is None:
        reminder.status = "completed"
        return

    next_utc = next_local.astimezone(timezone.utc)

    # The ID is assigned here rather than at flush time because the
    # delivery job's idempotency key is built from it.
    occurrence_id = uuid4()

    session.add(
        ReminderOccurrence(
            id=occurrence_id,
            reminder_id=reminder.id,
            scheduled_for=next_utc,
            status="scheduled",
        )
    )
    session.add(
        DeliveryJob(
            occurrence_id=occurrence_id,
            job_type=job_type,
            run_at=next_utc,
            status="pending",
            idempotency_key=f"reminder:{occurrence_id}",
        )
    )
    note_next_due(next_utc)


async def _recent_finance_activity(session, user_id, now: datetime) -> bool:
    """
    True when the user logged a transaction within the recent
    activity window. Any failure answers False (send the nudge):
    a broken check must never swallow a delivery.
    """
    try:
        from app.services import finance_service

        last = await finance_service.last_transaction_time(
            session,
            user_id,
        )
    except Exception:
        logger.warning(
            "Expense nudge activity check failed; sending anyway.",
            exc_info=True,
        )
        return False

    if last is None:
        return False

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    return now - last < RECENT_ACTIVITY_WINDOW


async def _deliver_one(session_factory, bot, job_id, *, now: datetime) -> str:
    """
    Deliver one claimed job in its own session.

    Returns "succeeded", "failed" or "cancelled". Never raises:
    one bad job must not stop the rest of the batch.
    """
    try:
        async with session_factory() as session:
            job = await session.get(DeliveryJob, job_id)

            if job is None or job.status != "processing":
                return "cancelled"

            occurrence = await session.get(
                ReminderOccurrence,
                job.occurrence_id,
            )
            reminder = (
                await session.get(Reminder, occurrence.reminder_id)
                if occurrence is not None
                else None
            )

            if occurrence is None or reminder is None:
                job.status = "cancelled"
                job.locked_until = None
                job.locked_by = None
                await session.commit()
                return "cancelled"

            if reminder.status == "cancelled":
                job.status = "cancelled"
                job.locked_until = None
                job.locked_by = None
                occurrence.status = "skipped"
                await session.commit()
                return "cancelled"

            job_type = getattr(job, "job_type", None) or "reminder"

            # Expense check-in nudges are pointless when the user
            # has just logged something: mark the occurrence
            # delivered without sending, and keep the schedule.
            if job_type == "expense_nudge" and (
                await _recent_finance_activity(
                    session,
                    reminder.user_id,
                    now,
                )
            ):
                session.add(
                    DeliveryAttempt(
                        job_id=job.id,
                        result="skipped",
                        error_message="recent activity",
                    )
                )
                job.status = "succeeded"
                job.locked_until = None
                job.locked_by = None
                job.last_error = None

                occurrence.status = "delivered"
                occurrence.completed_at = now

                if reminder.recurrence_rule:
                    _schedule_next_occurrence(
                        session,
                        reminder,
                        occurrence,
                        job_type=job_type,
                    )
                else:
                    reminder.status = "completed"

                await session.commit()
                return "succeeded"

            telegram_user_id = await session.scalar(
                select(TelegramAccount.telegram_user_id).where(
                    TelegramAccount.user_id == reminder.user_id
                )
            )

            if telegram_user_id is None:
                raise RuntimeError(
                    "Reminder owner has no Telegram account."
                )

            text = f"⏰ Reminder: {reminder.title}"
            if reminder.description:
                text += f"\n{reminder.description}"

            # The morning overview renders yesterday's money at
            # send time; the static reminder text is only a
            # fallback if building it fails.
            if job_type == "daily_overview":
                try:
                    from app.services import finance_service

                    overview = (
                        await finance_service.build_yesterday_overview(
                            session,
                            reminder.user_id,
                            ZoneInfo(reminder.timezone),
                            now,
                        )
                    )
                    if overview:
                        text = overview
                except Exception:
                    logger.warning(
                        "Failed to build daily overview for user %s; "
                        "sending the fallback reminder text.",
                        reminder.user_id,
                        exc_info=True,
                    )

            send_kwargs = {}
            if job_type in BUTTON_JOB_TYPES:
                send_kwargs["reply_markup"] = reminder_keyboard(
                    occurrence.id
                )

            try:
                try:
                    sent = await bot.send_message(
                        chat_id=telegram_user_id,
                        text=text,
                        **send_kwargs,
                    )
                except TypeError:
                    # Bot objects without reply_markup support
                    # (minimal test doubles): resend without the
                    # buttons rather than failing the delivery.
                    sent = await bot.send_message(
                        chat_id=telegram_user_id,
                        text=text,
                    )
            except Exception as exc:
                session.add(
                    DeliveryAttempt(
                        job_id=job.id,
                        result="failed",
                        error_code=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )
                )
                job.last_error = str(exc)[:500]

                if should_give_up(job.attempts, job.max_attempts):
                    job.status = "failed"
                else:
                    job.status = "pending"
                    job.run_at = now + retry_delay(job.attempts)

                job.locked_until = None
                job.locked_by = None
                await session.commit()
                return "failed"

            session.add(
                DeliveryAttempt(
                    job_id=job.id,
                    result="succeeded",
                    provider_message_id=(
                        str(sent.message_id)
                        if getattr(sent, "message_id", None) is not None
                        else None
                    ),
                )
            )
            job.status = "succeeded"
            job.locked_until = None
            job.locked_by = None
            job.last_error = None

            occurrence.status = "delivered"
            occurrence.completed_at = now

            if reminder.recurrence_rule:
                # A snooze extra is one-off: the series' next
                # occurrence is already scheduled, so nothing is
                # spawned from this delivery.
                if job_type != "reminder_snooze":
                    _schedule_next_occurrence(
                        session,
                        reminder,
                        occurrence,
                        job_type=job_type,
                    )
            else:
                reminder.status = "completed"

            await session.commit()
            return "succeeded"

    except Exception:
        logger.exception(
            "Unexpected failure delivering job %s",
            job_id,
        )
        return "failed"


async def process_due_deliveries(
    session_factory,
    bot,
    *,
    now: datetime | None = None,
    worker_id: str = "main",
    batch: int = 20,
) -> dict:
    """
    Claim due delivery jobs and deliver each one.

    Returns a small summary dict with the number of jobs claimed
    and how each ended. Safe to call on a timer: everything overdue
    (including reminders missed while the service slept) is picked
    up by the next call.
    """
    now = now or datetime.now(timezone.utc)

    async with session_factory() as session:
        job_ids = await _claim_due_jobs(
            session,
            now=now,
            worker_id=worker_id,
            batch=batch,
        )

    summary = {
        "claimed": len(job_ids),
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
    }

    for job_id in job_ids:
        outcome = await _deliver_one(
            session_factory,
            bot,
            job_id,
            now=now,
        )
        summary[outcome] = summary.get(outcome, 0) + 1

    if job_ids:
        logger.info("Delivery pass complete: %s", summary)

    return summary
