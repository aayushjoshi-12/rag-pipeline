from __future__ import annotations

import pytest
from app.services.health import CHECKS, CheckResult, run_health_checks


@pytest.mark.asyncio
async def test_run_health_checks_calls_each_probe_once() -> None:
    """Each check must run its own probe — regression guard against the
    generator late-binding bug that ran only the last probe for every check.
    """
    calls: list[str] = []

    async def probe(result: CheckResult, label: str) -> None:
        calls.append(label)
        result.ok = label == "ollama"
        result.detail = label

    fake_probes = {
        "db": lambda r: probe(r, "db"),
        "redis": lambda r: probe(r, "redis"),
        "chroma": lambda r: probe(r, "chroma"),
        "ollama": lambda r: probe(r, "ollama"),
    }

    import app.services.health as h

    orig_checks = h.CHECKS
    try:
        h.CHECKS = [
            (name, fake_probes[name], crit) for name, _p, crit in orig_checks
        ]
        results = await run_health_checks()
    finally:
        h.CHECKS = orig_checks

    assert calls == ["db", "redis", "chroma", "ollama"]
    by_name = {r.name: r for r in results}
    assert by_name["db"].ok is False
    assert by_name["redis"].ok is False
    assert by_name["chroma"].ok is False
    assert by_name["ollama"].ok is True


@pytest.mark.asyncio
async def test_run_health_checks_returns_one_result_per_check() -> None:
    results = await run_health_checks()
    assert {r.name for r in results} == {name for name, _p, _c in CHECKS}
    assert len(results) == len(CHECKS)