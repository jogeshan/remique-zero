from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.webhook import router as webhook_router
from app.config import settings
from app.database import close_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_database()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="A private, invite-only AI Telegram assistant.",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(webhook_router)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "running",
        "docs": "/docs",
    }
