from __future__ import annotations

import logging

import structlog


def configure_for_server(level: int = logging.INFO) -> None:
    # basicConfig is a no-op if a server (gunicorn/uvicorn) already attached handlers.
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger().setLevel(level)
    structlog.configure(
        processors=[
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


def configure_for_cli(level: int = logging.WARNING) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger().setLevel(level)
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
