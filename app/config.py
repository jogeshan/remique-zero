
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_name: str = "Remique Zero"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str = ""
    database_url_unpooled: str = ""

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_url: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_models: str = "gemini-3.8-flash"

    default_timezone: str = "Asia/Dhaka"
    max_ai_requests_per_user_per_day: int = 30
    super_admin_max_ai_requests_per_day: int = 300
    max_media_size_mb: int = 15

    job_poll_interval_seconds: int = 1
    job_max_attempts: int = 5
    job_lock_seconds: int = 120

    bootstrap_admin_telegram_id: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
