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


def usage_details(prompt_tokens: int | None, completion_tokens: int | None) -> dict[str, int]:
    """Langfuse ``usage_details`` payload from provider token counts."""
    details: dict[str, int] = {}
    if prompt_tokens is not None:
        details["input"] = prompt_tokens
    if completion_tokens is not None:
        details["output"] = completion_tokens
    return details
