from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog


def configure_for_server(level: int = logging.INFO) -> None:
    # basicConfig is a no-op if a server (gunicorn/uvicorn) already attached handlers.
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger().setLevel(level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields (request_id, job_id, doc_id, ...) onto every structlog event in this scope."""
    with structlog.contextvars.bound_contextvars(**fields):
        yield
