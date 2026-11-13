"""Langfuse generation tracing for backend LLM calls.

``generation`` is a context manager yielding an object with ``update(**kwargs)``. When
Langfuse has no credentials the client is a disabled no-op, so this is always safe to call.
Prompt/page-image content is only attached when ``_capture_content`` is on (opt-in via
``Settings.langfuse_capture_content``, applied by ``app.services.langfuse``); model names
and token usage always go out.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_capture_content = False


def capture_enabled() -> bool:
    return _capture_content


def maybe_content(value: Any) -> Any:
    """Return ``value`` only when content capture is enabled, else a redaction marker."""
    return value if _capture_content else "[content capture disabled]"


class _NullGeneration:
    def update(self, **_: Any) -> None:
        return None


@contextmanager
def generation(name: str, *, model: str, input: Any = None, **metadata: Any) -> Iterator[Any]:
    """Open a Langfuse generation span; degrade to a no-op if tracing is unavailable."""
    try:
        from langfuse import get_client

        cm = get_client().start_as_current_observation(
            name=name,
            as_type="generation",
            model=model,
            input=maybe_content(input),
            metadata=metadata or None,
        )
    except Exception:  # tracing must never break parsing
        logger.debug("Langfuse unavailable; skipping generation span", exc_info=True)
        yield _NullGeneration()
        return
    with cm as span:
        yield span


@contextmanager
def span(name: str, *, input: Any = None, **metadata: Any) -> Iterator[Any]:
    """Open a Langfuse span (nests under the current one); no-op if tracing is unavailable.

    ``input`` is passed through ``maybe_content`` so prompts/questions only leave the process
    when content capture is on. Exceptions are recorded on the span and re-raised.
    """
    try:
        from langfuse import get_client

        cm = get_client().start_as_current_observation(
            name=name,
            as_type="span",
            input=maybe_content(input) if input is not None else None,
            metadata=metadata or None,
        )
    except Exception:  # tracing must never break the request
        logger.debug("Langfuse unavailable; skipping span", exc_info=True)
        yield _NullGeneration()
        return
    with cm as sp:
        try:
            yield sp
        except Exception as exc:
            try:
                sp.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:500])
            except Exception:
                logger.debug("could not mark span as errored", exc_info=True)
            raise


def flush() -> None:
    """Best-effort flush of buffered spans (call on shutdown / end of a short-lived task)."""
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        logger.debug("Langfuse flush failed", exc_info=True)


def usage_details(prompt_tokens: int | None, completion_tokens: int | None) -> dict[str, int]:
    """Langfuse ``usage_details`` payload from provider token counts."""
    details: dict[str, int] = {}
    if prompt_tokens is not None:
        details["input"] = prompt_tokens
    if completion_tokens is not None:
        details["output"] = completion_tokens
    return details
