import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types
from sqlalchemy import func, select

from app.config import settings
from app.models import (
    AIUsageRecord,
    TelegramAccount,
    User,
    UserSetting,
)
from app.services.ai_tools import (
    TOOL_DECLARATIONS,
    execute_ai_tool,
)

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10
MAX_INPUT_CHARACTERS = 8000
MAX_OUTPUT_TOKENS = 900
MAX_TOOL_ROUNDS = 5


def build_system_instruction(
    role: str,
    display_name: str | None = None,
    timezone_name: str | None = None,
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
- Persistent conversation history.
- Administrative backend function calling.

NOT YET IMPLEMENTED:
- Reminder creation and scheduling.
- Automatic reminder delivery.
- Reminder snoozing and recurring schedules.
- Voice message processing.
- Image interpretation.

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

REMINDER RULES:

Reminder scheduling and automatic delivery are not implemented.
Never claim that a reminder has been saved or scheduled.

CONVERSATION RULES:

- Use previous conversation context when available.
- Respond in the user's language whenever practical.
- Be clear, concise, and helpful.
- Do not expose internal tool names unless useful for explanation.

You are an AI assistant connected to a Python backend.
Python services enforce permissions and perform actual operations.
"""

    role = role if role in role_descriptions else "user"

    return (
        "You are Remique Zero, a helpful personal AI assistant.\n\n"
        f"Authenticated user: {display_name or 'Telegram user'}\n"
        f"Authenticated role: {role}\n"
        f"User timezone: {timezone_name or 'Unknown'}\n\n"
        f"{role_descriptions[role]}\n"
        f"{capabilities}"
    )


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


def _build_contents(history: list[dict], prompt: str):
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

    contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=prompt)
            ],
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

                result = await execute_ai_tool(
                    session_factory,
                    telegram_user_id=telegram_user_id,
                    tool_name=function_name,
                    arguments=function_arguments,
                )

                function_response_parts.append(
                    types.Part.from_function_response(
                        name=function_name,
                        response=result,
                        id=function_id,
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


async def generate_ai_reply(
    session_factory,
    *,
    telegram_user_id: int,
    prompt: str,
    history: list[dict] | None = None,
) -> AIReply:

    if not settings.gemini_api_key:
        raise AIConfigurationError

    if not prompt or not prompt.strip():
        raise AIServiceError("Empty message.")

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

    started_at = perf_counter()
    selected_model = models[0]
    response = None

    try:
        contents = _build_contents(
            history or [],
            prompt.strip(),
        )

        system_instruction = build_system_instruction(
            role=user_role,
            display_name=display_name,
            timezone_name=timezone_name,
        )

        async with genai.Client(
            api_key=settings.gemini_api_key
        ).aio as client:

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