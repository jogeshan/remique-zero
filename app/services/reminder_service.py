import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from dateparser.search import search_dates
from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    DeliveryJob,
    Reminder,
    ReminderOccurrence,
    User,
)
from app.services.delivery_service import note_next_due

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 250

# The recurrence values accepted from users and the AI, mapped to the
# RRULE string stored on the reminder. Recurrence math is done with
# dateutil.rrule in the reminder's local timezone, so a "daily 9am"
# reminder stays at 9am local across UTC offset changes in most
# timezones; all persisted timestamps are UTC.
RECURRENCE_RULES: dict[str, str] = {
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
}

RECURRENCE_LABELS: dict[str, str] = {
    rule: label for label, rule in RECURRENCE_RULES.items()
}


def parse_recurrence(value) -> str | None:
    """
    Validate a recurrence value and return its RRULE string.

    Accepts None (one-time) or one of "daily", "weekly", "weekdays"
    (case-insensitive). Anything else raises ValueError.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError(
            "recurrence must be one of: daily, weekly, weekdays."
        )

    key = value.strip().lower()

    if key in ("", "none", "once"):
        return None

    try:
        return RECURRENCE_RULES[key]
    except KeyError:
        raise ValueError(
            "recurrence must be one of: daily, weekly, weekdays."
        ) from None


def recurrence_label(rrule_str: str | None) -> str | None:
    """Return the friendly recurrence name for a stored RRULE string."""
    if not rrule_str:
        return None

    return RECURRENCE_LABELS.get(rrule_str, rrule_str)


def compute_next_occurrence(
    first_run_at: datetime,
    rrule_str: str | None,
    after: datetime,
) -> datetime | None:
    """
    Return the next occurrence of a recurring schedule strictly
    after `after`, or None when there is no recurrence.

    `first_run_at` is the schedule's anchor (its first occurrence);
    the RRULE generates every later occurrence from it. The math runs
    in first_run_at's timezone, so callers that care about local
    wall-clock times should pass a local-aware anchor.
    """
    if not rrule_str:
        return None

    try:
        rule = rrulestr(rrule_str, dtstart=first_run_at)
    except (ValueError, TypeError):
        logger.warning("Invalid recurrence rule: %r", rrule_str)
        return None

    return rule.after(after, inc=False)


def _as_utc(value: datetime, fallback_tz: ZoneInfo) -> datetime:
    """Normalize a datetime to aware UTC (naive means fallback_tz)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=fallback_tz)

    return value.astimezone(timezone.utc)


async def create_reminder(
    session: AsyncSession,
    user: User,
    *,
    title: str,
    first_run_at: datetime,
    recurrence: str | None = None,
    description: str | None = None,
    timezone_name: str | None = None,
    job_type: str = "reminder",
) -> Reminder:
    """
    Create a reminder with its first occurrence and delivery job.

    `recurrence` is the friendly enum (None, "daily", "weekly",
    "weekdays"); the RRULE string is what gets stored. All times are
    persisted in UTC; the reminder keeps the user's timezone name so
    recurrence math and rendering can use local wall-clock time.
    `job_type` tags the delivery job; the delivery worker treats
    special types (expense nudges, the daily overview) differently
    from plain reminders.
    """
    tz_name = timezone_name or settings.default_timezone
    tz = ZoneInfo(tz_name)

    reminder = Reminder(
        user_id=user.id,
        title=title.strip()[:MAX_TITLE_LENGTH],
        description=description,
        timezone=tz_name,
        recurrence_rule=parse_recurrence(recurrence),
        status="active",
    )
    session.add(reminder)
    await session.flush()

    first_run_utc = _as_utc(first_run_at, tz)

    occurrence = ReminderOccurrence(
        reminder_id=reminder.id,
        scheduled_for=first_run_utc,
        status="scheduled",
    )
    session.add(occurrence)
    await session.flush()

    session.add(
        DeliveryJob(
            occurrence_id=occurrence.id,
            job_type=job_type,
            run_at=first_run_utc,
            status="pending",
            idempotency_key=f"reminder:{occurrence.id}",
        )
    )

    await session.commit()
    note_next_due(first_run_utc)
    return reminder


async def list_active_reminders(
    session: AsyncSession,
    user_id: UUID,
) -> list[tuple[Reminder, datetime | None]]:
    """
    Return the user's active reminders with each one's next
    scheduled occurrence time (None when none is scheduled).
    """
    reminders = list(
        await session.scalars(
            select(Reminder)
            .where(
                Reminder.user_id == user_id,
                Reminder.status == "active",
            )
            .order_by(Reminder.created_at.asc(), Reminder.id.asc())
        )
    )

    result: list[tuple[Reminder, datetime | None]] = []

    for reminder in reminders:
        next_at = await session.scalar(
            select(ReminderOccurrence.scheduled_for)
            .where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.status == "scheduled",
            )
            .order_by(ReminderOccurrence.scheduled_for.asc())
            .limit(1)
        )
        result.append((reminder, next_at))

    return result


async def _get_owned_reminder(
    session: AsyncSession,
    user_id: UUID,
    reminder_id: UUID,
) -> Reminder | None:
    """Load a reminder only when it belongs to the given user."""
    return await session.scalar(
        select(Reminder).where(
            Reminder.id == reminder_id,
            Reminder.user_id == user_id,
        )
    )


async def cancel_reminder(
    session: AsyncSession,
    user_id: UUID,
    reminder_id: UUID,
) -> bool:
    """
    Cancel a reminder owned by the user.

    Its still-scheduled occurrences are skipped and their pending
    delivery jobs cancelled, so the delivery worker never sends them.
    Returns False when no such reminder exists for this user.
    """
    reminder = await _get_owned_reminder(session, user_id, reminder_id)

    if reminder is None:
        return False

    reminder.status = "cancelled"

    occurrences = list(
        await session.scalars(
            select(ReminderOccurrence).where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.status == "scheduled",
            )
        )
    )

    for occurrence in occurrences:
        occurrence.status = "skipped"

        jobs = await session.scalars(
            select(DeliveryJob).where(
                DeliveryJob.occurrence_id == occurrence.id,
                DeliveryJob.status.in_(["pending", "processing"]),
            )
        )
        for job in jobs:
            job.status = "cancelled"
            job.locked_until = None
            job.locked_by = None

    await session.commit()
    return True


async def snooze_occurrence(
    session: AsyncSession,
    user_id: UUID,
    reminder_id: UUID,
    minutes: int,
) -> bool:
    """
    Move a reminder's next scheduled occurrence forward by `minutes`.

    The occurrence's pending delivery job moves with it. Returns
    False when the user has no such active reminder or it has no
    scheduled occurrence left to move.
    """
    reminder = await _get_owned_reminder(session, user_id, reminder_id)

    if reminder is None or reminder.status != "active":
        return False

    occurrence = await session.scalar(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.reminder_id == reminder.id,
            ReminderOccurrence.status == "scheduled",
        )
        .order_by(ReminderOccurrence.scheduled_for.asc())
        .limit(1)
    )

    if occurrence is None:
        return False

    delay = timedelta(minutes=minutes)
    occurrence.scheduled_for = occurrence.scheduled_for + delay

    job = await session.scalar(
        select(DeliveryJob).where(
            DeliveryJob.occurrence_id == occurrence.id,
            DeliveryJob.status == "pending",
        )
    )
    if job is not None:
        job.run_at = job.run_at + delay

    await session.commit()
    return True


async def get_reminder_occurrence(
    session: AsyncSession,
    occurrence_id: UUID,
) -> tuple[ReminderOccurrence, Reminder] | None:
    """
    Load an occurrence together with its reminder, or None.

    Used by the reminder action buttons, which address an
    occurrence directly; ownership is the caller's to check
    against reminder.user_id.
    """
    occurrence = await session.get(ReminderOccurrence, occurrence_id)

    if occurrence is None:
        return None

    reminder = await session.get(Reminder, occurrence.reminder_id)

    if reminder is None:
        return None

    return occurrence, reminder


async def mark_occurrence_done(
    session: AsyncSession,
    user_id: UUID,
    occurrence_id: UUID,
) -> bool:
    """
    Mark a delivered occurrence as done (the ✅ Done button).

    Only the occurrence itself changes: for a recurring reminder
    the already-scheduled next occurrence stays untouched, and a
    one-time reminder is completed. Returns False when the
    occurrence does not exist, belongs to another user, or was
    not in the delivered state.
    """
    pair = await get_reminder_occurrence(session, occurrence_id)

    if pair is None:
        return False

    occurrence, reminder = pair

    if reminder.user_id != user_id:
        return False

    if occurrence.status != "delivered":
        return False

    occurrence.status = "done"
    occurrence.completed_at = datetime.now(timezone.utc)

    if not reminder.recurrence_rule and reminder.status == "active":
        reminder.status = "completed"

    await session.commit()
    return True


async def snooze_delivered_occurrence(
    session: AsyncSession,
    user_id: UUID,
    occurrence_id: UUID,
    minutes: int,
    *,
    now: datetime | None = None,
) -> bool:
    """
    Snooze the occurrence an action button was attached to.

    A still-scheduled occurrence is simply moved forward by
    `minutes`, together with its pending delivery job (the same
    semantics as snooze_occurrence). A delivered occurrence can
    no longer be moved, so a fresh one-off occurrence and
    delivery job are created at now + `minutes` instead; a
    one-time reminder that delivery marked completed becomes
    active again. The extra job carries job_type
    "reminder_snooze", which tells the delivery worker not to
    spawn the next series occurrence from it, so snoozing a
    recurring reminder never disturbs its regular schedule.
    Returns False when the occurrence does not exist, belongs
    to another user, or is already done/skipped.
    """
    pair = await get_reminder_occurrence(session, occurrence_id)

    if pair is None:
        return False

    occurrence, reminder = pair

    if reminder.user_id != user_id:
        return False

    delay = timedelta(minutes=minutes)

    if occurrence.status == "scheduled":
        if reminder.status != "active":
            return False

        occurrence.scheduled_for = occurrence.scheduled_for + delay

        job = await session.scalar(
            select(DeliveryJob).where(
                DeliveryJob.occurrence_id == occurrence.id,
                DeliveryJob.status == "pending",
            )
        )
        if job is not None:
            job.run_at = job.run_at + delay

        await session.commit()
        return True

    if occurrence.status != "delivered":
        return False

    if reminder.recurrence_rule:
        if reminder.status != "active":
            return False
    elif reminder.status not in ("active", "completed"):
        return False

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    run_at = now + delay

    # The ID is assigned here rather than at flush time because
    # the delivery job's idempotency key is built from it (the
    # same pattern the delivery worker uses).
    new_occurrence_id = uuid4()

    session.add(
        ReminderOccurrence(
            id=new_occurrence_id,
            reminder_id=reminder.id,
            scheduled_for=run_at,
            status="scheduled",
        )
    )
    session.add(
        DeliveryJob(
            occurrence_id=new_occurrence_id,
            job_type="reminder_snooze",
            run_at=run_at,
            status="pending",
            idempotency_key=f"reminder:{new_occurrence_id}",
        )
    )

    if not reminder.recurrence_rule:
        reminder.status = "active"

    await session.commit()
    note_next_due(run_at)
    return True


# --- /remind text parsing -------------------------------------------
#
# parse_reminder_text is a deliberately pragmatic, deterministic
# parser for the /remind command. It understands one date/time
# expression per message ("tomorrow at 9am", "in 2 hours",
# "on Sunday at 18:30", a bare "at 9pm") plus the recurrence
# keywords "every day"/"daily", "every week"/"weekly",
# "every <weekday>" and "every weekday". It does NOT understand
# date ranges, multiple reminders in one message, or relative
# phrases dateparser cannot find; when no date/time is found it
# returns None and the caller asks the user to rephrase. The AI
# chat path does not use this parser — Gemini resolves times
# itself and calls the create_reminder tool with an ISO timestamp.

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_WEEKDAY_PATTERN = "|".join(_WEEKDAYS)

_RECURRENCE_DETECTORS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bevery\s+weekdays?\b|\bon\s+weekdays\b"), "weekdays"),
    (re.compile(r"\bevery\s+day\b|\bdaily\b|\beach\s+day\b"), "daily"),
    (re.compile(r"\bevery\s+week\b|\bweekly\b"), "weekly"),
    (re.compile(rf"\bevery\s+({_WEEKDAY_PATTERN})\b"), "weekly"),
]

# Phrases removed from the title once a recurrence is detected.
# Bare "daily"/"weekly" are deliberately NOT removed: they are
# often part of the thing itself ("the weekly report", "daily
# standup"), and the recurrence is already captured separately.
_RECURRENCE_PHRASES = re.compile(
    rf"\bevery\s+(day|week|weekdays?|{_WEEKDAY_PATTERN})\b"
    r"|\beach\s+day\b|\bon\s+weekdays\b",
    re.IGNORECASE,
)

_TIME_OF_DAY = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))\s*(am|pm)?\b"
    r"|\b(\d{1,2})\s*(am|pm)\b",
    re.IGNORECASE,
)


def _detect_recurrence(text: str) -> str | None:
    lowered = text.lower()
    for pattern, value in _RECURRENCE_DETECTORS:
        if pattern.search(lowered):
            return value
    return None


def _parse_time_match(match: re.Match) -> tuple[int, int]:
    """Extract (hour, minute) in 24h time from a _TIME_OF_DAY match."""
    if match.group(4) is not None:
        hour = int(match.group(4))
        minute = 0
        meridiem = match.group(5)
    else:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3)

    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0

    return hour % 24, minute % 60


def _first_run_from_time(
    now_local: datetime,
    hour: int,
    minute: int,
    recurrence: str | None,
    weekday_name: str | None,
) -> datetime:
    """
    Build the first run for a bare time-of-day ("at 9am") combined
    with an optional recurrence, always landing strictly in the
    future on a day the recurrence allows.
    """
    candidate = now_local.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    if recurrence == "weekdays":
        while candidate <= now_local or candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    elif recurrence == "weekly":
        target = _WEEKDAYS.get(weekday_name or "", now_local.weekday())
        candidate += timedelta(
            days=(target - candidate.weekday()) % 7
        )
        if candidate <= now_local:
            candidate += timedelta(days=7)
    else:
        # One-time and daily both start at the next future slot.
        if candidate <= now_local:
            candidate += timedelta(days=1)

    return candidate


def _extract_title(text: str, matched_date_text: str | None) -> str:
    """Best-effort removal of the time/recurrence phrases."""
    title = text

    if matched_date_text:
        title = title.replace(matched_date_text, " ", 1)

    title = _RECURRENCE_PHRASES.sub(" ", title)
    # Removing the date text can orphan the word "every" that
    # preceded it ("... report every [Sunday at 18:30]").
    title = re.sub(r"\bevery\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(
        r"^\s*remind\s+me\s+(to\s+)?",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s+", " ", title).strip(" ,.-")
    title = re.sub(
        r"^(at|on|in|by|to)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s+(at|on|in|by)$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = title.strip(" ,.-")

    return title or "Reminder"


def parse_reminder_text(
    text: str,
    user_tz: ZoneInfo,
    now_local: datetime,
) -> tuple[str, datetime, str | None] | None:
    """
    Parse /remind text into (title, first run local datetime,
    recurrence enum or None). Returns None when no date or time
    can be found. See the section comment above for its limits.
    """
    if not text or not text.strip():
        return None

    cleaned = text.strip()
    recurrence = _detect_recurrence(cleaned)

    matched_text: str | None = None
    candidate: datetime | None = None

    try:
        matches = search_dates(
            cleaned,
            settings={"RELATIVE_BASE": now_local},
        )
    except Exception:
        logger.warning(
            "dateparser failed on reminder text; "
            "falling back to time-of-day parsing.",
            exc_info=True,
        )
        matches = None

    if matches:
        matched_text, parsed = matches[0]
        if parsed.tzinfo is None:
            candidate = parsed.replace(tzinfo=user_tz)
        else:
            candidate = parsed.astimezone(user_tz)

    if candidate is None:
        time_match = _TIME_OF_DAY.search(cleaned)
        if time_match is None:
            return None

        hour, minute = _parse_time_match(time_match)
        weekday_match = re.search(
            rf"\b({_WEEKDAY_PATTERN})\b",
            cleaned,
            flags=re.IGNORECASE,
        )
        candidate = _first_run_from_time(
            now_local,
            hour,
            minute,
            recurrence,
            weekday_match.group(1).lower() if weekday_match else None,
        )
        matched_text = time_match.group(0)
    elif recurrence is not None:
        # A parsed time can land in the past ("at 7am" said at
        # 10am) or on a day the recurrence excludes (a Sunday for
        # "every weekday"); roll forward to the next valid slot.
        rrule_str = parse_recurrence(recurrence)
        off_pattern = recurrence == "weekdays" and candidate.weekday() >= 5
        if candidate <= now_local or off_pattern:
            next_run = compute_next_occurrence(
                candidate,
                rrule_str,
                now_local,
            )
            if next_run is not None:
                candidate = next_run
    elif candidate <= now_local:
        # Bare one-time time earlier today ("at 9pm" said at 10pm):
        # the user almost certainly means the next one, tomorrow.
        if candidate.date() == now_local.date():
            candidate += timedelta(days=1)

    title = _extract_title(cleaned, matched_text)

    return title, candidate, recurrence
