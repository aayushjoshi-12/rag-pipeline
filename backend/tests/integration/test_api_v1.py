"""/v1 documents: pagination envelope, the legacy bare-array alias, error envelope on real routes (real Postgres)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from app.config import Settings
from app.db import ops
from app.db.models import Document
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import api_client

N = 7


@pytest.fixture
async def client(db_session: AsyncSession, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    cfg = Settings(data_dir=tmp_path, auth_enabled=False)

    async with api_client(db_session, settings=lambda: cfg) as c:
        yield c


@pytest.fixture
async def seeded(db_session: AsyncSession) -> AsyncIterator[list[str]]:
    # the throwaway DB is shared by the whole session: start from (and leave) an empty documents table
    await db_session.execute(delete(Document))
    await db_session.commit()
    ids = []
    for i in range(N):
        d = await ops.create_document(
            filename=f"doc-{i}.md", content_type="text/markdown", session=db_session
        )
        ids.append(str(d.id))
    yield ids  # created order; the API lists newest first
    await db_session.execute(delete(Document))
    await db_session.commit()


async def test_v1_list_is_a_documented_envelope(
    client: httpx.AsyncClient, seeded: list[str]
) -> None:
    r = await client.get("/v1/documents")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "total", "limit", "offset", "has_more"}
    assert (
        body["total"] == N
        and body["limit"] == 50
        and body["offset"] == 0
        and body["has_more"] is False
    )
    assert [d["id"] for d in body["items"]] == list(reversed(seeded))
    assert "deprecation" not in r.headers


async def test_pages_are_disjoint_ordered_and_complete(
    client: httpx.AsyncClient, seeded: list[str]
) -> None:
    seen: list[str] = []
    offset = 0
    while True:
        page = (await client.get("/v1/documents", params={"limit": 3, "offset": offset})).json()
        assert page["limit"] == 3 and page["offset"] == offset and page["total"] == N
        assert len(page["items"]) <= 3
        seen += [d["id"] for d in page["items"]]
        assert page["has_more"] == (offset + len(page["items"]) < N)
        if not page["has_more"]:
            break
        offset += 3
    assert seen == list(reversed(seeded))  # every document exactly once, newest first


async def test_offset_past_the_end_is_an_empty_page(
    client: httpx.AsyncClient, seeded: list[str]
) -> None:
    page = (await client.get("/v1/documents", params={"offset": 100})).json()
    assert page["items"] == [] and page["total"] == N and page["has_more"] is False


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 501}, {"limit": -1}, {"offset": -1}, {"limit": "x"}]
)
async def test_out_of_range_paging_is_a_422_not_a_silent_clamp(
    client: httpx.AsyncClient, params: dict[str, object]
) -> None:
    r = await client.get("/v1/documents", params=params)
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"


async def test_limit_500_is_accepted(client: httpx.AsyncClient, seeded: list[str]) -> None:
    assert (await client.get("/v1/documents", params={"limit": 500})).json()["limit"] == 500


async def test_legacy_unversioned_list_keeps_its_bare_array_shape(
    client: httpx.AsyncClient, seeded: list[str]
) -> None:
    r = await client.get("/documents")
    assert r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) == N
    assert r.headers["deprecation"] == "true"
    assert r.headers["link"] == '</v1/documents>; rel="successor-version"'
    assert len((await client.get("/documents", params={"skip": 5})).json()) == N - 5


async def test_not_found_uses_the_error_envelope(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/v1/documents/{uuid.uuid4()}", headers={"X-Request-ID": "doc-404"})
    assert r.status_code == 404
    assert r.json() == {
        "detail": "Document not found.",
        "code": "not_found",
        "request_id": "doc-404",
    }
    bad = await client.get("/v1/documents/not-a-uuid")
    assert bad.status_code == 422 and bad.json()["code"] == "validation_error"


async def test_v1_and_legacy_share_the_other_document_routes(
    client: httpx.AsyncClient, seeded: list[str]
) -> None:
    one = seeded[0]
    new = (await client.get(f"/v1/documents/{one}")).json()
    old = (await client.get(f"/documents/{one}")).json()
    assert new == old and new["id"] == one
