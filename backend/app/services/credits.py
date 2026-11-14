"""OpenRouter key balance (``GET /key``), read server-side so the API key never reaches the browser."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)

CACHE_SECONDS = 60.0
FAILURE_CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class Credits:
    available: bool
    #: Why there is no balance: ``provider_not_openrouter`` | ``no_api_key`` | ``unavailable``.
    reason: str | None = None
    limit: float | None = None  # None = the key has no spending limit
    limit_remaining: float | None = None
    usage: float | None = None
    is_free_tier: bool | None = None
    fetched_at: float | None = None


_cache: tuple[float, str, Credits] | None = None  # (expires_at, cache key, value)


def reset_cache() -> None:
    global _cache
    _cache = None


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


async def get_credits(settings: Settings, client: httpx.AsyncClient | None = None) -> Credits:
    """The key's limit/usage, cached for 60 s. Never raises: failures become ``available=False``."""
    global _cache
    if settings.llm_provider != "openrouter":
        return Credits(False, "provider_not_openrouter")
    if not settings.openrouter_api_key:
        return Credits(False, "no_api_key")
    key = f"{settings.openrouter_base_url}|{hash(settings.openrouter_api_key)}"
    now = time.monotonic()
    if _cache and _cache[1] == key and _cache[0] > now:
        return _cache[2]
    try:
        async with client or httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get(
                f"{settings.openrouter_base_url.rstrip('/')}/key",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            )
        resp.raise_for_status()
        data = resp.json()["data"]
        value = Credits(
            True,
            limit=_num(data.get("limit")),
            limit_remaining=_num(data.get("limit_remaining")),
            usage=_num(data.get("usage")),
            is_free_tier=data.get("is_free_tier") if isinstance(data.get("is_free_tier"), bool) else None,
            fetched_at=time.time(),
        )
        ttl = CACHE_SECONDS
    except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as exc:
        # log the exception type only: its text can embed the request URL
        logger.warning("credits_unavailable", error=type(exc).__name__)
        value, ttl = Credits(False, "unavailable"), FAILURE_CACHE_SECONDS
    _cache = (now + ttl, key, value)
    return value
