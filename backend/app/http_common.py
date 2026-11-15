"""Cross-cutting HTTP behaviour: request ids, the error envelope, the deprecated-alias headers.

* Every response carries ``X-Request-ID`` (the client's, when it is a safe token; else a fresh one).
  It is bound to the structlog context and written to one access-log line per request.
* Every error body is ``{"detail": ..., "code": "<stable machine code>", "request_id": "..."}``.
  ``detail`` keeps its historic shape (string, or the pydantic error list for 422), so existing
  clients keep working; ``code`` and ``request_id`` are additive.
* Unversioned paths (the pre-``/v1`` routes) answer with ``Deprecation: true`` and a ``Link``
  header pointing at the ``/v1`` successor.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.version import API_PREFIX

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "x-request-id"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
#: Paths that are not part of the versioned API and so are never marked deprecated.
_UNVERSIONED_OK = ("/health", "/docs", "/redoc", "/openapi.json")

#: Stable machine-readable error codes by HTTP status. Part of the API contract: do not rename.
ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


class ErrorResponse(BaseModel):
    """The body of every error response."""

    detail: Any = Field(
        description="Human-readable message (string); for `422` the list of validation problems.",
        examples=["Document not found."],
    )
    code: str = Field(
        description="Stable machine-readable error code, derived from the HTTP status "
        "(`bad_request`, `unauthorized`, `forbidden`, `not_found`, `conflict`, `payload_too_large`, "
        "`unsupported_media_type`, `validation_error`, `service_unavailable`, `internal_error`, ...).",
        examples=["not_found"],
    )
    request_id: str = Field(
        description="Echo of the `X-Request-ID` response header; quote it when reporting a problem.",
        examples=["3f0c9c3e6c534d4e9f550b0f7a1d2a10"],
    )


def error_code(status_code: int) -> str:
    if status_code in ERROR_CODES:
        return ERROR_CODES[status_code]
    return "client_error" if status_code < 500 else "server_error"


def _request_id(scope: Scope) -> str:
    return str(scope.get("request_id", ""))


def error_response(
    scope: Scope, status_code: int, detail: Any, headers: dict[str, str] | None = None
) -> JSONResponse:
    rid = _request_id(scope)
    out = dict(headers or {})
    if rid:
        out["X-Request-ID"] = rid
    return JSONResponse(
        {"detail": detail, "code": error_code(status_code), "request_id": rid},
        status_code=status_code,
        headers=out,
    )


class RequestContextMiddleware:
    """Pure ASGI (no body buffering, safe for streaming): request id, access log, deprecation headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = ""
        for name, value in scope.get("headers", []):
            if name == REQUEST_ID_HEADER.encode():
                supplied = value.decode("latin-1")
                break
        rid = supplied if _SAFE_ID.match(supplied) else uuid.uuid4().hex
        scope["request_id"] = rid
        path: str = scope.get("path", "")
        deprecated = not (
            path == API_PREFIX
            or path.startswith(API_PREFIX + "/")
            or path in _UNVERSIONED_OK
            or path.startswith(("/docs/", "/redoc/"))
        )
        started = time.perf_counter()
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                if not any(h[0].lower() == REQUEST_ID_HEADER.encode() for h in headers):
                    headers.append((REQUEST_ID_HEADER.encode(), rid.encode()))
                if deprecated:
                    successor = f'<{API_PREFIX}{path}>; rel="successor-version"'
                    headers.append((b"deprecation", b"true"))
                    headers.append((b"link", successor.encode("latin-1", "replace")))
                message = {**message, "headers": headers}
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=rid):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                logger.info(
                    "request_id=%s method=%s path=%s status=%s duration_ms=%d",
                    rid,
                    scope.get("method", "-"),
                    path,
                    status_code or "-",
                    (time.perf_counter() - started) * 1000,
                )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        headers = dict(exc.headers) if exc.headers else None
        return error_response(request.scope, exc.status_code, exc.detail, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request.scope, status.HTTP_422_UNPROCESSABLE_CONTENT, jsonable_encoder(exc.errors())
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).error(
            "unhandled error request_id=%s", _request_id(request.scope), exc_info=exc
        )
        return error_response(request.scope, 500, "Internal server error.")
