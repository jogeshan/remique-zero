import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from time import perf_counter
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types
from sqlalchemy import func, select

from app.config import settings
from app.models import (
    AIUsageRecord,
    Conversation,
    TelegramAccount,
    User,
    UserSetting,
)
from app.services import conversation_service, memory_service
from app.services.ai_tools import (
    TOOL_DECLARATIONS,
    execute_ai_tool,
)

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10
MAX_INPUT_CHARACTERS = 8000
MAX_OUTPUT_TOKENS = 900
MAX_TOOL_ROUNDS = 5

# Tools that change backend state. Within a single AI request, an
# identical repeat call reuses the first result instead of executing
# again, so a duplicated function call cannot create duplicate
# invitations or repeat an account change.
MUTATING_TOOLS = {
    "create_invite",
    "revoke_invite",
    "set_user_status",
    "change_user_role",
    "add_transaction",
    "add_debt",
    "record_debt_payment",
    "set_expense_reminders",
    "set_daily_overview",
    "create_task",
    "complete_task",
    "delete_task",
}

# Human-readable names for the language codes offered in
# /settings, used in the system instruction.
LANGUAGE_NAMES = {
    "bn": "Bangla (Bengali)",
}


def build_system_instruction(
    role: str,
    display_name: str | None = None,
    timezone_name: str | None = None,
    memories_text: str | None = None,
    now_local: datetime | None = None,
    language: str | None = None,
    conversation_summary: str | None = None,
) -> str:

    role_descriptions = {
        "super_admin": """
You are speaking with the SUPER ADMIN of Remique Zero.

The Super Admin has the highest application-level privileges.

They can:
- Create and revoke invitation codes.
- View user statistics.
- List registered users.
- Enable and disable user accounts.
- Promote users to administrators.
- Demote administrators to regular users.

You can perform these operations using the backend tools provided
to you. When the Super Admin asks you to perform an available
operation, call the appropriate tool instead of merely explaining
how to do it.
""",
        "admin": """
You are speaking with an ADMIN of Remique Zero.

Admins can:
- Create and revoke invitation codes.
- View user statistics.
- List registered users.
- Enable and disable regular user accounts.

Admins cannot promote or demote administrators.

Use backend tools to perform available operations.
Do not merely explain how to perform an operation when a tool
is available and the authenticated role is authorized.
""",
        "user": """
You are speaking with a REGULAR USER of Remique Zero.

Provide personal assistance and explain available features.

You cannot perform administrator operations.
Do not attempt to use administrator tools for regular users.
""",
    }

    capabilities = """
REMlQUE ZERO — BACKEND CAPABILITIES

IMPLEMENTED:
- Telegram registration and authentication.
- Invitation creation and revocation.
- User and administrator management.
- Account activation and deactivation.
- Gemini text conversations.
- AI request limits and usage tracking.
- Database-backed user accounts.
- Persistent conversation history (use /newchat to start fresh; the previous conversation is preserved).
- Administrative backend function calling.
- Persistent personal memory: every user can ask you to remember, recall, and forget personal facts and preferences, stored per user across conversations.
- Reminders: every user can create one-time and recurring (daily, weekly, weekdays) reminders in their own timezone, delivered as Telegram messages. Reminders that come due while the service is asleep are delivered when it wakes.
- Multimodal input: users can send photos and images for you to interpret, voice messages and audio (Bangla, English, or Banglish — the message is transcribed for you and you respond in the same language the user used), and PDF or plain-text documents for you to read, summarize, and answer questions about.
- Expense tracking: every user can log income and expenses by category, track money they borrowed or lent (debts and loans) with partial repayments, and get totals and breakdowns per period. Users can enable expense check-in reminders several times a day and a morning overview of yesterday's money.
- Personal task lists: every user has a to-do list they can add to, review, complete, and delete from in conversation.

ADMINISTRATIVE TOOL RULES:

1. Use the available backend tools for administrative operations.
2. Never claim an action succeeded until a tool confirms success.
3. Never invent user IDs, invitation codes, statistics, or account data.
4. Never ask the user to provide their own Telegram ID as proof of role.
5. The backend authenticates the acting Telegram user independently.
6. Never attempt to bypass backend permission checks.
7. Never reveal internal credentials, API keys, or secrets.
8. When a tool reports failure, explain the actual failure.
9. Never claim an unavailable feature is implemented.

INVITATION RULES:

- When asked to create an invitation, call create_invite.
- Use one maximum use unless the user requests another amount.
- If an expiration period is requested, pass it in days.
- Return the invitation code only when the backend successfully creates it.
- Never invent an invitation code.

USER MANAGEMENT RULES:

- Use get_user_statistics for statistics.
- Use list_users when the user asks to see registered users.
- Use set_user_status to enable or disable an account.
- Use change_user_role to promote or demote an account.
- If a required target Telegram ID is missing, ask for it.

MEMORY RULES:

- You can remember, recall, and forget personal facts for the user
  you are speaking with, using the remember_fact, recall_memories,
  and forget_memory tools. These work for every role, but only ever
  on that user's own memory.
- When the user explicitly asks you to remember something (for
  example "remember that ..."), call remember_fact instead of only
  acknowledging it in text.
- When the user asks what you remember about them, call
  recall_memories.
- When the user asks you to forget something, call recall_memories
  first if you need the memory ID, then forget_memory.
- Never invent memories. Only state facts that a tool returned or
  that appear in the stored-memory section of these instructions.

REMINDER RULES:

- Reminders are implemented. When the user asks to be reminded
  about something, call create_reminder instead of only
  acknowledging it in text.
- Resolve relative times ("tomorrow", "in 2 hours", "next
  Sunday") against the current date and time given in these
  instructions, in the user's timezone, and pass first_run_at
  as an ISO 8601 timestamp WITH the timezone offset (for
  example 2026-11-16T09:00:00+06:00).
- After creating a reminder, confirm it back to the user with
  the local date and time from the tool result.
- Use list_reminders to show the user's reminders,
  cancel_reminder to cancel one, and snooze_reminder to delay
  one. These tools only ever operate on the speaking user's
  own reminders.
- Recurrence must be one of: daily, weekly, weekdays. Omit it
  for one-time reminders.
- Never claim a reminder was created until the tool confirms
  success.

FINANCE RULES:

- Expense tracking is implemented. When the user mentions
  spending, earning, borrowing, or lending money, call the
  finance tools instead of only acknowledging it in text.
- add_transaction records money movement: type is "income" or
  "expense". Choose the category from the context of what the
  user said. Expense categories: Food, Transport, Shopping,
  Bills, Rent, Health, Entertainment, Education, Family, Other.
  Income categories: Salary, Business, Freelance, Gift, Other.
- Amounts are in BDT (taka) unless the user says another
  currency.
- After adding a transaction, confirm briefly with the amount
  and category from the tool result.
- For questions like "how much did I spend this month?", use
  get_finance_summary or list_transactions. Never invent
  amounts, totals, or transactions.
- Debts and loans: "borrowed" means the user owes the other
  person; "lent" means the other person owes the user. Use
  add_debt when money is borrowed or lent, record_debt_payment
  when a debt is (partly) repaid, and list_debts to show what
  is still open.
- Expense check-ins: set_expense_reminders turns the daily
  "log your expenses" nudges on or off (1 to 8 times a day).
- Daily overview: get_daily_overview shows the money overview
  for yesterday (or today); set_daily_overview manages the
  automatic morning delivery of yesterday's overview.

TASK RULES:

- Task lists are implemented. Every user has a personal to-do
  list, managed with create_task, list_tasks, complete_task,
  and delete_task. These tools only ever operate on the
  speaking user's own list.
- When the user asks to add something to their list ("add to
  my list", "put X on my to-do list"), call create_task
  instead of only acknowledging it in text.
- When the user asks what is on their list, call list_tasks.
- When the user says something on the list is done ("mark X
  done", "I finished X"), call complete_task with the task ID
  from list_tasks; call list_tasks first if you need the ID.
- Tasks are NOT reminders: a task's due date is informational
  only and nothing is delivered at that time. If the user
  wants to be notified at a time, create a reminder as well
  (or instead), following the REMINDER RULES.

CONVERSATION RULES:

- Use previous conversation context when available.
- Respond in the user's language whenever practical.
- Be clear, concise, and helpful.
- Do not expose internal tool names unless useful for explanation.

You are an AI assistant connected to a Python backend.
Python services enforce permissions and perform actual operations.
"""

    role = role if role in role_descriptions else "user"

    instruction = (
        "You are Remique Zero, a helpful personal AI assistant.\n\n"
        f"Authenticated user: {display_name or 'Telegram user'}\n"
        f"Authenticated role: {role}\n"
        f"User timezone: {timezone_name or 'Unknown'}\n"
    )

    if now_local is not None:
        instruction += (
            "Current date and time for the user: "
            f"{now_local.strftime('%A %d %B %Y, %H:%M:%S')} "
            f"({timezone_name or 'Unknown'})\n"
        )

    instruction += (
        f"\n{role_descriptions[role]}\n"
        f"{capabilities}"
    )

    if memories_text:
        instruction += (
            "\n\nWhat you remember about this user:\n"
            f"{memories_text}\n\n"
            "This is stored memory from earlier conversations. "
            "Use it when it is relevant, and tell the user they can "
            "ask you to update or delete any of it at any time."
        )

    if language and language != "en":
        language_name = LANGUAGE_NAMES.get(language, language)
        instruction += (
            f"\n\nThe user's preferred language is {language_name}. "
            "Prefer replying in that language unless the user "
            "writes in a different language."
        )

    if conversation_summary:
        instruction += (
            "\n\nSummary of earlier in this conversation:\n"
            f"{conversation_summary}\n\n"
            "This summary covers the part of the conversation "
            "that scrolled out of the recent history below. "
            "Treat it as established context from this same "
            "conversation."
        )

    return instruction


class AIServiceError(Exception):
    """Base exception for AI service failures."""


class AIConfigurationError(AIServiceError):
    """Gemini configuration is missing or invalid."""


class AIAccountInactive(AIServiceError):
    """The Telegram account is not active."""


class AIQuotaExceeded(AIServiceError):
    """The user's daily AI request allowance is exhausted."""


class AIProviderError(AIServiceError):
    """The AI provider could not complete the request."""


class AIToolFlowError(Exception):
    """
    A failure occurred after Gemini requested an administrative tool.

    Model fallback must not restart the request because an operation
    may already have produced a side effect.
    """


@dataclass(frozen=True)
class AIReply:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


def _get_user_timezone(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or settings.default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _get_model_candidates() -> list[str]:
    primary = getattr(
        settings,
        "gemini_model",
        "gemini-3.5-flash-lite",
    )

    fallback_value = getattr(
        settings,
        "gemini_fallback_models",
        "gemini-3.8-flash",
    )

    models = [primary.strip()] if primary.strip() else []

    fallbacks = [
        model.strip()
        for model in fallback_value.split(",")
        if model.strip()
    ]

    for model in fallbacks:
        if model not in models:
            models.append(model)

    return models


def _should_failover(exc: Exception) -> bool:
    """
    Allow fallback for temporary provider failures and unavailable models.
    Do not hide authentication, permission, or malformed-request errors.
    """

    status_code = getattr(exc, "code", None)

    if status_code is None:
        status_code = getattr(exc, "status_code", None)

    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        status_code = None

    if status_code in (404, 408, 429, 500, 502, 503, 504):
        return True

    error_name = type(exc).__name__.lower()

    retryable_names = (
        "servererror",
        "serviceunavailable",
        "deadlineexceeded",
        "timeoutexception",
        "connecterror",
        "readtimeout",
        "connecttimeout",
        "toomanyrequests",
        "notfound",
    )

    return any(name in error_name for name in retryable_names)


def _build_contents(
    history: list[dict],
    prompt: str,
    attachments: list[tuple[str, bytes]] | None = None,
):
    contents = []

    for item in history[-MAX_HISTORY_MESSAGES:]:
        role = item.get("role")
        message_text = item.get("text")

        if role not in ("user", "model"):
            continue

        if not isinstance(message_text, str) or not message_text.strip():
            continue

        contents.append(
            types.Content(
                role=role,
                parts=[
                    types.Part.from_text(text=message_text)
                ],
            )
        )

    user_parts = []

    # Attachments belong to this user turn only. They stay in the
    # contents list for the whole request, so the tool-calling loop
    # keeps seeing the same media on every round.
    for mime_type, data in attachments or []:
        user_parts.append(
            types.Part.from_bytes(
                data=data,
                mime_type=mime_type,
            )
        )

    user_parts.append(
        types.Part.from_text(text=prompt)
    )

    contents.append(
        types.Content(
            role="user",
            parts=user_parts,
        )
    )

    return contents


async def _reserve_request(
    session_factory,
    telegram_user_id: int,
):
    async with session_factory() as session:
        result = await session.execute(
            select(User, UserSetting.timezone)
            .join(
                TelegramAccount,
                TelegramAccount.user_id == User.id,
            )
            .outerjoin(
                UserSetting,
                UserSetting.user_id == User.id,
            )
            .where(
                TelegramAccount.telegram_user_id == telegram_user_id,
                User.deleted_at.is_(None),
            )
        )

        row = result.one_or_none()

        if row is None:
            raise AIAccountInactive

        user, timezone_name = row

        if user.status != "active":
            raise AIAccountInactive

        user_role = user.role
        display_name = user.display_name

        user_timezone = _get_user_timezone(timezone_name)

        local_now = datetime.now(timezone.utc).astimezone(
            user_timezone
        )

        local_start = datetime.combine(
            local_now.date(),
            time.min,
            tzinfo=user_timezone,
        )

        local_end = datetime.combine(
            local_now.date() + timedelta(days=1),
            time.min,
            tzinfo=user_timezone,
        )

        start_utc = local_start.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)

        lock_key = user.id.int & 0x7FFFFFFFFFFFFFFF

        await session.execute(
            select(func.pg_advisory_xact_lock(lock_key))
        )

        request_count = await session.scalar(
            select(func.count())
            .select_from(AIUsageRecord)
            .where(
                AIUsageRecord.user_id == user.id,
                AIUsageRecord.created_at >= start_utc,
                AIUsageRecord.created_at < end_utc,
            )
        )

        if user_role == "super_admin":
            request_limit = settings.super_admin_max_ai_requests_per_day
        else:
            request_limit = settings.max_ai_requests_per_user_per_day

        if (request_count or 0) >= request_limit:
            await session.rollback()
            raise AIQuotaExceeded

        usage = AIUsageRecord(
            user_id=user.id,
            provider="google",
            model=settings.gemini_model,
            operation="chat",
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0.0,
            success=False,
        )

        session.add(usage)
        await session.flush()

        usage_id = usage.id

        await session.commit()

        return (
            usage_id,
            user_role,
            display_name,
            timezone_name,
        )


async def _finish_usage(
    session_factory,
    usage_id,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    success: bool,
) -> None:
    try:
        async with session_factory() as session:
            usage = await session.get(AIUsageRecord, usage_id)

            if usage is None:
                return

            usage.model = model
            usage.input_tokens = input_tokens
            usage.output_tokens = output_tokens
            usage.latency_ms = latency_ms
            usage.success = success

            await session.commit()

    except Exception:
        logger.exception(
            "Unable to finalize AI usage record: %s",
            usage_id,
        )


def _build_gemini_tools():
    declarations = [
        types.FunctionDeclaration(
            name=item["name"],
            description=item["description"],
            parameters_json_schema=item["parameters_json_schema"],
        )
        for item in TOOL_DECLARATIONS
    ]

    return [
        types.Tool(
            function_declarations=declarations,
        )
    ]


def _get_function_calls(response):
    calls = getattr(response, "function_calls", None)
    return list(calls or [])


def _get_response_text(response) -> str:
    answer = getattr(response, "text", None)

    if isinstance(answer, str):
        return answer.strip()

    return ""


async def _generate_with_tools(
    client,
    *,
    model: str,
    contents: list,
    system_instruction: str,
    session_factory,
    telegram_user_id: int,
):
    """
    Run Gemini's function-calling conversation loop.

    Returns:
        (response, tool_was_requested)

    Once a function call has been received, any later exception is
    wrapped in AIToolFlowError to prevent model fallback from repeating
    a potentially completed administrative operation.
    """

    tool_was_requested = False
    executed_mutating: dict = {}

    try:
        for round_number in range(MAX_TOOL_ROUNDS + 1):

            response = await client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.4,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    tools=_build_gemini_tools(),
                ),
            )

            function_calls = _get_function_calls(response)

            if not function_calls:
                return response, tool_was_requested

            tool_was_requested = True

            if round_number >= MAX_TOOL_ROUNDS:
                raise RuntimeError(
                    "Gemini exceeded the maximum tool-call rounds."
                )

            candidate = None
            candidates = getattr(response, "candidates", None) or []

            if candidates:
                candidate = getattr(candidates[0], "content", None)

            if candidate is not None:
                contents.append(candidate)

            function_response_parts = []

            for function_call in function_calls:
                function_name = getattr(
                    function_call,
                    "name",
                    None,
                )

                function_arguments = getattr(
                    function_call,
                    "args",
                    None,
                ) or {}

                function_id = getattr(
                    function_call,
                    "id",
                    None,
                )

                if not function_name:
                    raise RuntimeError(
                        "Gemini returned a function call without a name."
                    )

                logger.info(
                    "Gemini requested tool: name=%s actor=%s",
                    function_name,
                    telegram_user_id,
                )

                dedupe_key = None
                if function_name in MUTATING_TOOLS:
                    dedupe_key = (
                        function_name,
                        json.dumps(
                            function_arguments,
                            sort_keys=True,
                            default=str,
                        ),
                    )

                if dedupe_key is not None and dedupe_key in executed_mutating:
                    logger.info(
                        "Duplicate AI tool call skipped: name=%s actor=%s",
                        function_name,
                        telegram_user_id,
                    )
                    result = executed_mutating[dedupe_key]
                else:
                    result = await execute_ai_tool(
                        session_factory,
                        telegram_user_id=telegram_user_id,
                        tool_name=function_name,
                        arguments=function_arguments,
                    )

                    if dedupe_key is not None:
                        executed_mutating[dedupe_key] = result

                # NOTE: Part.from_function_response() does not accept an
                # `id` argument in google-genai 1.x — passing one raises
                # TypeError after the tool has already executed. Build the
                # FunctionResponse directly, which does support `id`.
                function_response_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=function_name,
                            response=result,
                            id=function_id,
                        )
                    )
                )

            contents.append(
                types.Content(
                    role="user",
                    parts=function_response_parts,
                )
            )

        raise RuntimeError(
            "Gemini did not finish the tool conversation."
        )

    except AIToolFlowError:
        raise

    except Exception as exc:
        if tool_was_requested:
            raise AIToolFlowError(
                "The AI tool workflow failed after a tool call."
            ) from exc

        raise


async def _load_memories_text(
    session_factory,
    telegram_user_id: int,
) -> str | None:
    """
    Load the user's stored memories, formatted for the system prompt.

    Memory is an enhancement, never a dependency: any failure is
    logged and treated as "no memories" so chat keeps working.
    """
    try:
        async with session_factory() as session:
            user = await memory_service.get_active_user_by_telegram_id(
                session,
                telegram_user_id,
            )

            if user is None:
                return None

            memories = await memory_service.get_memories_for_context(
                session,
                user.id,
            )

        return memory_service.format_memories_for_prompt(memories) or None

    except Exception:
        logger.exception(
            "Unable to load memories for Telegram user %s; "
            "continuing without them.",
            telegram_user_id,
        )
        return None


async def _load_user_language(
    session_factory,
    telegram_user_id: int,
) -> str | None:
    """
    Load the user's preferred language from their settings.

    Like memories, this is an enhancement, never a dependency:
    any failure is logged and treated as "no preference" so
    chat keeps working.
    """
    try:
        async with session_factory() as session:
            user = await memory_service.get_active_user_by_telegram_id(
                session,
                telegram_user_id,
            )

            if user is None:
                return None

            return await session.scalar(
                select(UserSetting.language).where(
                    UserSetting.user_id == user.id
                )
            )

    except Exception:
        logger.exception(
            "Unable to load language for Telegram user %s; "
            "continuing without it.",
            telegram_user_id,
        )
        return None


def _get_summary_model() -> str | None:
    """
    The cheapest model candidate, used for background
    summarization: the "lite" tier when one is configured,
    otherwise the primary candidate.
    """
    models = _get_model_candidates()

    for model in models:
        if "lite" in model.lower():
            return model

    return models[0] if models else None


async def _load_conversation_summary(
    session_factory,
    conversation_id: UUID,
) -> tuple[str | None, list[dict] | None]:
    """
    Load a conversation's rolling summary and its recent
    unsummarized history.

    Like memories, this is an enhancement, never a dependency:
    any failure is logged and treated as "no summary" so chat
    keeps working with the caller-provided history.
    """
    try:
        async with session_factory() as session:
            conversation = await session.get(
                Conversation,
                conversation_id,
            )

            if conversation is None:
                return None, None

            summary, recent_history = (
                await conversation_service.get_history_with_summary(
                    session,
                    conversation,
                )
            )

            return summary, recent_history

    except Exception:
        logger.exception(
            "Unable to load conversation summary for %s; "
            "continuing without it.",
            conversation_id,
        )
        return None, None


async def _maybe_compress_conversation(
    client,
    session_factory,
    conversation_id: UUID,
) -> str | None:
    """
    Fold the oldest unsummarized messages into the rolling
    summary when the conversation has grown past the threshold.

    Returns the updated summary, or None when no compression
    was due. FULLY failure-safe: any error is logged and the
    caller continues with the plain recent history — compression
    must never break a reply.
    """
    try:
        async with session_factory() as session:
            conversation = await session.get(
                Conversation,
                conversation_id,
            )

            if conversation is None:
                return None

            total = (
                await conversation_service
                .count_conversation_messages(
                    session,
                    conversation_id,
                )
            )
            summarized = conversation.summary_message_count or 0

            if not conversation_service.should_compress(
                total,
                summarized,
            ):
                return None

            previous_summary = conversation.summary
            chunk = (
                await conversation_service
                .get_oldest_unsummarized_messages(
                    session,
                    conversation,
                )
            )

        if not chunk:
            return None

        model = _get_summary_model()

        if model is None:
            return None

        prompt = conversation_service.build_summary_prompt(
            previous_summary,
            chunk,
        )

        # Plain text call on the cheapest model — no tools, so a
        # summary can never trigger a backend operation.
        response = await client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1200,
            ),
        )

        new_summary = conversation_service.cap_summary(
            _get_response_text(response)
        )

        if not new_summary:
            return None

        async with session_factory() as session:
            conversation = await session.get(
                Conversation,
                conversation_id,
            )

            if conversation is None:
                return None

            await conversation_service.save_conversation_summary(
                session,
                conversation,
                new_summary,
                len(chunk),
            )

        logger.info(
            "Conversation context compressed: conversation=%s "
            "folded=%s",
            conversation_id,
            len(chunk),
        )

        return new_summary

    except Exception:
        logger.exception(
            "Conversation compression failed for %s; "
            "continuing without it.",
            conversation_id,
        )
        return None


async def generate_ai_reply(
    session_factory,
    *,
    telegram_user_id: int,
    prompt: str,
    history: list[dict] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
    conversation_id: UUID | None = None,
) -> AIReply:

    if not settings.gemini_api_key:
        raise AIConfigurationError

    prompt = (prompt or "").strip()

    if not prompt and not attachments:
        raise AIServiceError("Empty message.")

    if not prompt:
        # Media with no caption or instruction: fall back to a
        # generic request so the model still gets a text part.
        prompt = "Please look at the attached media and respond helpfully."

    if len(prompt) > MAX_INPUT_CHARACTERS:
        raise AIServiceError("Message is too long.")

    models = _get_model_candidates()

    if not models:
        raise AIConfigurationError

    (
        usage_id,
        user_role,
        display_name,
        timezone_name,
    ) = await _reserve_request(
        session_factory,
        telegram_user_id,
    )

    memories_text = await _load_memories_text(
        session_factory,
        telegram_user_id,
    )

    language = await _load_user_language(
        session_factory,
        telegram_user_id,
    )

    # When the caller identifies the conversation, prefer its
    # summary-aware history: the rolling summary goes into the
    # system instruction and the summarized messages stay out of
    # the contents. Loading is failure-safe — on any problem the
    # caller-provided history is used as before.
    conversation_summary = None

    if conversation_id is not None:
        loaded_summary, recent_history = (
            await _load_conversation_summary(
                session_factory,
                conversation_id,
            )
        )

        conversation_summary = loaded_summary

        if recent_history is not None:
            history = recent_history

    started_at = perf_counter()
    selected_model = models[0]
    response = None

    try:
        contents = _build_contents(
            history or [],
            prompt,
            attachments=attachments,
        )

        now_local = datetime.now(
            _get_user_timezone(timezone_name)
        )

        async with genai.Client(
            api_key=settings.gemini_api_key
        ).aio as client:

            # Fold old messages into the rolling summary when
            # the conversation outgrows the threshold. This is
            # best-effort: it never fails the reply, it only
            # costs one extra cheap-model call on the requests
            # where it triggers.
            if conversation_id is not None:
                updated_summary = (
                    await _maybe_compress_conversation(
                        client,
                        session_factory,
                        conversation_id,
                    )
                )

                if updated_summary:
                    conversation_summary = updated_summary

            system_instruction = build_system_instruction(
                role=user_role,
                display_name=display_name,
                timezone_name=timezone_name,
                memories_text=memories_text,
                now_local=now_local,
                language=language,
                conversation_summary=conversation_summary,
            )

            for index, model in enumerate(models):
                selected_model = model

                try:
                    logger.info(
                        "Attempting AI request: model=%s role=%s",
                        model,
                        user_role,
                    )

                    response, tool_was_requested = (
                        await _generate_with_tools(
                            client,
                            model=model,
                            contents=list(contents),
                            system_instruction=system_instruction,
                            session_factory=session_factory,
                            telegram_user_id=telegram_user_id,
                        )
                    )

                    if response is not None:
                        break

                except AIToolFlowError:
                    # Never restart a request after an administrative
                    # tool may already have executed.
                    raise

                except Exception as exc:
                    logger.warning(
                        "AI model failed: model=%s error=%s",
                        model,
                        type(exc).__name__,
                    )

                    has_fallback = index < len(models) - 1

                    if not has_fallback or not _should_failover(exc):
                        raise

                    logger.warning(
                        "Switching AI model: failed=%s next=%s",
                        model,
                        models[index + 1],
                    )

        if response is None:
            raise RuntimeError(
                "No AI model returned a response."
            )

        answer = _get_response_text(response)

        if not answer:
            raise RuntimeError(
                "AI model returned an empty response."
            )

        usage_metadata = getattr(
            response,
            "usage_metadata",
            None,
        )

        input_tokens = (
            getattr(usage_metadata, "prompt_token_count", 0) or 0
        )

        output_tokens = (
            getattr(usage_metadata, "candidates_token_count", 0) or 0
        )

        latency_ms = int(
            (perf_counter() - started_at) * 1000
        )

        await _finish_usage(
            session_factory,
            usage_id,
            model=selected_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            success=True,
        )

        logger.info(
            "AI response completed: model=%s latency_ms=%s "
            "input_tokens=%s output_tokens=%s role=%s",
            selected_model,
            latency_ms,
            input_tokens,
            output_tokens,
            user_role,
        )

        return AIReply(
            text=answer,
            model=selected_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    except Exception as exc:
        latency_ms = int(
            (perf_counter() - started_at) * 1000
        )

        await _finish_usage(
            session_factory,
            usage_id,
            model=selected_model,
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            success=False,
        )

        logger.error(
            "AI request failed: telegram_user_id=%s model=%s "
            "error=%s status=%s latency_ms=%s",
            telegram_user_id,
            selected_model,
            type(exc).__name__,
            getattr(exc, "code", None),
            latency_ms,
        )

        raise AIProviderError from exc