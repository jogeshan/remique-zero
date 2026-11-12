from fastapi import APIRouter

from app.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    return {
        "status": "ok",
        "application": settings.app_name,
        "environment": settings.app_env,
    }
