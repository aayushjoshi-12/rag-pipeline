"""Vendor-facing HTTP behaviour on a freshly built app: CORS preflight, request ids, error envelope, aliases.

No database: only routes that fail before touching it (auth, validation, 404 of unknown routes) or that
the test adds itself are used.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from app.config import Settings
from app.main import build_app
from app.routers import deps
from fastapi import FastAPI, HTTPException

ORIGIN = "https://app.example.com"


def make_app(**overrides: object) -> FastAPI:
    cfg = Settings(auth_enabled=True, secret_key="x" * 40, **overrides)  # type: ignore[arg-type]  # kwargs are Settings fields
    application = build_app(cfg)
    application.dependency_overrides[deps.get_settings] = lambda: cfg

    @application.get("/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("kaboom with internal detail")

    @application.get("/v1/_teapot")
    async def teapot() -> None:
        raise HTTPException(409, "already there")

    return application


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(), raise_app_exceptions=False),
        base_url="http://t",
    ) as c:
        yield c


def cors_client(origins: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=make_app(cors_allow_origins=origins), raise_app_exceptions=False
        ),
        base_url="http://t",
    )


def preflight(
    origin: str = ORIGIN,
    method: str = "POST",
    headers: str = "authorization,content-type,x-api-key",
) -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    }


# ── CORS ────────────────────────────────────────────────────────────────────────


async def test_preflight_from_allowed_origin_succeeds_without_credentials() -> None:
    async with cors_client(f"{ORIGIN},https://other.example.com") as c:
        r = await c.options(
            "/v1/search", headers=preflight()
        )  # no Authorization: preflights never carry it
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in r.headers["access-control-allow-methods"]
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "x-api-key" in allowed and "authorization" in allowed and "content-type" in allowed
    assert "access-control-allow-credentials" not in r.headers
    assert r.headers["x-request-id"]


async def test_preflight_from_unlisted_origin_is_refused() -> None:
    async with cors_client(ORIGIN) as c:
        r = await c.options("/v1/search", headers=preflight(origin="https://evil.example.org"))
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


async def test_preflight_with_disallowed_header_is_refused() -> None:
    async with cors_client(ORIGIN) as c:
        r = await c.options("/v1/search", headers=preflight(headers="x-internal-secret"))
    assert r.status_code == 400


async def test_preflight_with_disallowed_method_is_refused() -> None:
    async with cors_client(ORIGIN) as c:
        r = await c.options("/v1/search", headers=preflight(method="TRACE"))
    assert r.status_code == 400


async def test_default_has_no_cors_at_all(client: httpx.AsyncClient) -> None:
    pre = await client.options("/v1/search", headers=preflight())
    assert not any(h.startswith("access-control-") for h in pre.headers)
    assert pre.status_code != 200  # the browser's preflight fails: cross-origin is off by default
    simple = await client.get("/v1/capabilities", headers={"Origin": ORIGIN})
    assert not any(h.startswith("access-control-") for h in simple.headers)


async def test_actual_request_gets_allow_origin_and_exposes_headers() -> None:
    async with cors_client(ORIGIN) as c:
        r = await c.get("/v1/capabilities", headers={"Origin": ORIGIN})
    assert r.status_code == 401  # auth still applies; CORS headers are on the error too
    assert r.headers["access-control-allow-origin"] == ORIGIN
    exposed = r.headers["access-control-expose-headers"].lower()
    assert "x-request-id" in exposed and "x-cache" in exposed


async def test_wildcard_origin_allows_anyone_but_never_credentials() -> None:
    async with cors_client("*") as c:
        r = await c.options("/v1/search", headers=preflight(origin="https://anything.example.net"))
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers


def test_invalid_cors_origin_setting_is_rejected() -> None:
    for bad in ("app.example.com", "https://app.example.com/path", "ftp://x"):
        with pytest.raises(ValueError, match="CORS_ALLOW_ORIGINS"):  # pydantic wraps SettingsError
            Settings(cors_allow_origins=bad)
    assert Settings(
        cors_allow_origins=" https://a.example.com/ , https://b.example.com:8443 "
    ).cors_origins == (
        "https://a.example.com",
        "https://b.example.com:8443",
    )
    assert Settings(cors_allow_origins="").cors_origins == ()


# ── request id + error envelope ───────────────────────────────────────────────────


async def test_request_id_is_generated_and_echoed(client: httpx.AsyncClient) -> None:
    r1 = await client.get("/v1/capabilities")
    r2 = await client.get("/v1/capabilities")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
    mine = await client.get("/v1/capabilities", headers={"X-Request-ID": "trace-abc_123.9"})
    assert mine.headers["x-request-id"] == "trace-abc_123.9"
    assert mine.json()["request_id"] == "trace-abc_123.9"


async def test_unsafe_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/capabilities", headers={"X-Request-ID": "bad id; with spaces"})
    assert r.headers["x-request-id"] != "bad id; with spaces"
    assert len(r.headers["x-request-id"]) == 32
    r = await client.get("/v1/capabilities", headers={"X-Request-ID": "x" * 200})
    assert len(r.headers["x-request-id"]) == 32


async def test_request_id_is_in_the_access_log(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="app.access"):
        await client.get("/v1/capabilities", headers={"X-Request-ID": "log-me-1"})
    lines = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert any(
        "request_id=log-me-1" in m and "status=401" in m and "path=/v1/capabilities" in m
        for m in lines
    )


async def test_http_error_envelope_keeps_detail_and_adds_code_and_request_id(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/v1/capabilities", headers={"X-Request-ID": "rid-1"})
    assert r.status_code == 401
    assert r.json() == {
        "detail": "not authenticated",
        "code": "unauthorized",
        "request_id": "rid-1",
    }
    assert r.headers["www-authenticate"] == "Bearer"  # exception headers survive the handler

    r = await client.get("/v1/_teapot")
    assert (
        r.status_code == 409
        and r.json()["code"] == "conflict"
        and r.json()["detail"] == "already there"
    )

    r = await client.get("/v1/nope")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert r.json()["request_id"] == r.headers["x-request-id"]


async def test_validation_error_envelope(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/auth/login", json={"email": "a@example.com"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error" and body["request_id"] == r.headers["x-request-id"]
    assert isinstance(body["detail"], list) and body["detail"][0]["loc"][-1] == "password"


async def test_unhandled_error_is_a_500_envelope_without_internals(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/v1/_boom", headers={"X-Request-ID": "rid-boom"})
    assert r.status_code == 500
    assert r.json() == {
        "detail": "Internal server error.",
        "code": "internal_error",
        "request_id": "rid-boom",
    }
    assert "kaboom" not in r.text
    assert r.headers["x-request-id"] == "rid-boom"


# ── versioning ────────────────────────────────────────────────────────────────────


async def test_unversioned_alias_works_but_is_marked_deprecated() -> None:
    cfg = Settings(auth_enabled=False)
    application = build_app(cfg)
    application.dependency_overrides[deps.get_settings] = lambda: cfg
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://t"
    ) as c:
        old = await c.get("/capabilities")
        new = await c.get("/v1/capabilities")
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()
    assert old.headers["deprecation"] == "true"
    assert old.headers["link"] == '</v1/capabilities>; rel="successor-version"'
    assert "deprecation" not in new.headers


# ── capabilities ──────────────────────────────────────────────────────────────────


async def test_capabilities_needs_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/capabilities")).status_code == 401


async def test_capabilities_content_and_no_secrets() -> None:
    cfg = Settings(
        auth_enabled=False,
        secret_key="s3cr3t-signing-key-0123456789abcdef0123",
        openrouter_api_key="sk-or-very-secret",
        admin_password="hunter2-admin",
        s3_access_key_id="AKIAEXAMPLEKEYID",
        s3_secret_access_key="s3-secret-material",
        database_url="postgresql+asyncpg://u:dbpassword@h/db",
        max_upload_size_bytes=1234,
        cors_allow_origins=ORIGIN,
    )
    application = build_app(cfg)
    application.dependency_overrides[deps.get_settings] = lambda: cfg
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://t"
    ) as c:
        r = await c.get("/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] and body["api_versions"] == ["v1"] and body["auth_enabled"] is False
    assert body["retrieval"]["modes"] == ["semantic", "keyword", "hybrid"]
    assert body["retrieval"]["fusions"] == ["rrf", "weighted"]
    assert body["retrieval"]["keyword_methods"] == ["bm25", "postgres_fts"]
    assert body["vector_store"]["store"] == "chroma" and body["vector_store"]["metric"] == "l2"
    assert body["uploads"]["max_bytes"] == 1234
    assert body["uploads"]["extensions"] == ["docx", "md", "pdf", "txt"]
    assert body["pagination"] == {"default_limit": 50, "max_limit": 500}
    assert body["features"]["cors_enabled"] is True and body["features"]["url_ingest"] is True
    text = r.text
    for secret in (
        "s3cr3t-signing",
        "sk-or-very-secret",
        "hunter2",
        "s3-secret-material",
        "dbpassword",
        "postgresql",
    ):
        assert secret not in text
