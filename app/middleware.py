"""
HTTP hardening middleware (Phase 12): security headers and a
simple in-memory fixed-window rate limiter.

No external dependencies and no shared state beyond this process,
which matches the single-instance Render Free deployment. The
limiter counts per client IP; behind Render's proxy the first
`X-Forwarded-For` entry is the real client address.
"""

import threading
import time

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The dashboard page is a single self-contained document with
    # inline CSS/JS, so those two directives allow 'unsafe-inline';
    # everything else falls back to 'self'.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; "
        "img-src 'self' data:"
    ),
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the security headers to every response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)

        return response


class FixedWindowRateLimiter:
    """
    Fixed-window counter: at most `limit` calls per key per
    `window_seconds`. The clock is injectable for tests.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        clock=time.monotonic,
    ):
        if limit < 1:
            raise ValueError("Rate limit must be at least 1.")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()

        with self._lock:
            if len(self._buckets) > 10_000:
                self._sweep(now)

            window_start, count = self._buckets.get(key, (now, 0))

            if now - window_start >= self.window_seconds:
                window_start, count = now, 0

            count += 1
            self._buckets[key] = (window_start, count)
            return count <= self.limit

    def _sweep(self, now: float) -> None:
        expired = [
            key
            for key, (start, _) in self._buckets.items()
            if now - start >= self.window_seconds
        ]
        for key in expired:
            del self._buckets[key]


def _client_key(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limits for the two HTTP entry points that accept
    outside traffic: the dashboard API and the Telegram webhook.
    Everything else (health checks, the dashboard page, docs) is
    not limited. Disable with RATE_LIMIT_ENABLED=false.
    """

    def __init__(self, app):
        super().__init__(app)
        self.api_limiter = FixedWindowRateLimiter(
            settings.api_rate_limit_per_minute
        )
        self.webhook_limiter = FixedWindowRateLimiter(
            settings.webhook_rate_limit_per_minute
        )

    async def dispatch(self, request, call_next):
        if not settings.rate_limit_enabled:
            return await call_next(request)

        path = request.url.path
        limiter = None

        if path.startswith("/api/v1"):
            limiter = self.api_limiter
        elif path == "/telegram/webhook":
            limiter = self.webhook_limiter

        if limiter is not None and not limiter.allow(
            _client_key(request)
        ):
            return JSONResponse(
                {
                    "error": (
                        "Rate limit exceeded. "
                        "Please wait a moment and try again."
                    )
                },
                status_code=429,
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
