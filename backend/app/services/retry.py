"""Retry helper for per-page backend calls."""

from __future__ import annotations

from collections.abc import Callable

from tenacity import (
    Retrying as _Retrying,
)
from tenacity import (
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.services.parse_errors import BackendCallError

MAX_ATTEMPTS = 3


def with_retry[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Call ``fn`` retrying transient failures (``BackendCallError``, ``TimeoutError``).

    Up to ``MAX_ATTEMPTS`` tries with exponential backoff; the last error is re-raised.
    The wait strategy is built per call so tests can patch ``wait_exponential``.
    """
    retrying = _Retrying(
        retry=retry_if_exception_type((BackendCallError, TimeoutError)),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    return retrying(fn, *args, **kwargs)
