import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db_session

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check():
    """Cheap liveness check — no database access."""
    return {
        "status": "ok",
        "application": settings.app_name,
        "environment": settings.app_env,
    }


def _alembic_head() -> str | None:
    """
    The head revision from the migration scripts on disk.

    Returns None when it cannot be determined; callers treat that
    as "unknown", never as a failure by itself.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(__file__).resolve().parents[2] / "migrations"),
        )
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        logger.warning(
            "Could not determine the Alembic head revision.",
            exc_info=True,
        )
        return None


@router.get("/health/deep")
async def health_deep(
    session: AsyncSession = Depends(get_db_session),
):
    """
    Readiness check: database connectivity plus migration state.

    Never raises — any failure is reported as status "degraded"
    with a generic detail (raw driver errors are logged, not
    returned, so no connection details leak to callers).
    """
    database_status = "ok"
    detail = None
    current_revision = None

    try:
        await session.execute(text("SELECT 1"))

        try:
            current_revision = await session.scalar(
                text(
                    "SELECT version_num FROM alembic_version "
                    "LIMIT 1"
                )
            )
        except Exception:
            logger.warning(
                "Deep health check: alembic_version is unreadable.",
                exc_info=True,
            )
            current_revision = None
    except Exception:
        logger.exception("Deep health check: database unreachable.")
        database_status = "error"
        detail = "Database check failed."

    head_revision = _alembic_head()
    up_to_date = None
    if head_revision is not None and current_revision is not None:
        up_to_date = head_revision == current_revision

    status = "ok"
    if database_status != "ok":
        status = "degraded"
    elif up_to_date is False:
        status = "degraded"
        detail = (
            "Database migrations are not up to date "
            f"(database: {current_revision}, expected: "
            f"{head_revision})."
        )

    return {
        "status": status,
        "application": settings.app_name,
        "environment": settings.app_env,
        "database": database_status,
        "alembic": {
            "head": head_revision,
            "current": current_revision,
            "up_to_date": up_to_date,
        },
        "detail": detail,
    }
