from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.routers.deps import get_settings
from app.services.health import run_health_checks

router = APIRouter()


@router.get("/health")
async def health() -> Response:
    checks = await run_health_checks()
    payload: dict[str, Any] = {
        "status": "ok",
        # Public on purpose (no secret): lets the dashboard skip the login form / the /auth/me probe correctly.
        "auth_enabled": (await get_settings()).auth_enabled,
        "checks": {
            c.name: {"ok": c.ok, "detail": c.detail} for c in checks
        },
    }
    # Overall unhealthy if any critical check failed.
    if not all(c.ok for c in checks if c.critical):
        payload["status"] = "degraded"
        return Response(
            content=_json_dumps(payload),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )
    return Response(
        content=_json_dumps(payload),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
    )


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload)