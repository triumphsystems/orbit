import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from typing import ClassVar

from fastapi import HTTPException, Request, Response, status

from core.config.settings import get_settings


class APIRateLimiter:
    """
    Provider-agnostic sliding-window rate limiter for API endpoints.
    Protects LLM inference, orchestration runs, and expensive adapter endpoints from quota drain.
    """

    _requests: ClassVar[dict[str, list[float]]] = defaultdict(list)
    _lock: ClassVar[asyncio.Lock | None] = None
    TRUSTED_PROXIES: ClassVar[set[str]] = {
        "127.0.0.1",
        "::1",
        "localhost",
        "testclient",
    }

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    def get_client_ip(cls, request: Request) -> str:
        """Extracts the client IP address, honoring proxy headers only when received from trusted upstream proxies."""
        direct_host = request.client.host if request.client and request.client.host else "127.0.0.1"

        # Check if direct connecting host is a trusted upstream proxy or local container network
        is_trusted = (
            direct_host in cls.TRUSTED_PROXIES
            or direct_host.startswith("10.")
            or direct_host.startswith("172.16.")
            or direct_host.startswith("192.168.")
        )
        if is_trusted:
            cf_ip = request.headers.get("CF-Connecting-IP")
            if cf_ip:
                return cf_ip.strip()
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
            real_ip = request.headers.get("X-Real-IP")
            if real_ip:
                return real_ip.strip()

        return direct_host

    @classmethod
    async def check(
        cls,
        client_id: str,
        resource_key: str,
        limit: int,
        window_seconds: int = 60,
    ) -> tuple[bool, int, int]:
        """
        Checks if the request is allowed under the sliding window.
        Returns (is_allowed, remaining_quota, retry_after_seconds).
        """
        now = time.time()
        window_start = now - window_seconds
        bucket_key = f"{client_id}:{resource_key}"

        async with cls._get_lock():
            # Filter out timestamps outside the active sliding window and purge stale entries to avoid leaks
            active_ts = [
                ts for ts in cls._requests.get(bucket_key, []) if ts > window_start
            ]
            if not active_ts and bucket_key in cls._requests:
                del cls._requests[bucket_key]
            elif active_ts:
                cls._requests[bucket_key] = active_ts

            current_count = len(cls._requests.get(bucket_key, []))
            if current_count >= limit:
                oldest_ts = cls._requests[bucket_key][0]
                retry_after = max(1, int(window_seconds - (now - oldest_ts)))
                return False, 0, retry_after

            # Record this request timestamp
            cls._requests[bucket_key].append(now)
            remaining = max(0, limit - current_count - 1)
            return True, remaining, 0

    @classmethod
    def reset(cls) -> None:
        """Resets all tracked request buckets (useful for test isolation)."""
        cls._requests.clear()


def rate_limit(
    resource_key: str,
    max_requests: int | None = None,
    window_seconds: int = 60,
) -> Callable:
    """
    FastAPI dependency for endpoint-level sliding window rate limiting.
    """

    async def _dependency(request: Request, response: Response) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return

        # Resolve limit from settings if not explicitly provided
        effective_limit = max_requests
        if effective_limit is None:
            if resource_key == "goal":
                effective_limit = settings.rate_limit_goal_per_minute
            elif resource_key == "run":
                effective_limit = settings.rate_limit_run_per_minute
            else:
                effective_limit = settings.rate_limit_default_per_minute

        client_ip = APIRateLimiter.get_client_ip(request)
        allowed, remaining, retry_after = await APIRateLimiter.check(
            client_id=client_ip,
            resource_key=resource_key,
            limit=effective_limit,
            window_seconds=window_seconds,
        )

        response.headers["X-RateLimit-Limit"] = str(effective_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        if not allowed:
            response.headers["Retry-After"] = str(retry_after)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for endpoint '{resource_key}'. "
                    f"Maximum {effective_limit} request(s) per {window_seconds}s allowed. "
                    f"Please retry in {retry_after} second(s)."
                ),
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(effective_limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

    return _dependency
