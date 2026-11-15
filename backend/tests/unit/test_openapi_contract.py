"""OpenAPI contract: the committed docs/openapi.json must match the app, and breaking changes need an ack.

``test_committed_snapshot_*`` is the CI gate. The rest pin the breaking-change rules themselves on small
synthetic specs, so a weakened detector fails here even while the real spec is unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from app import openapi_contract as oc
from app.main import app

Json = dict[str, Any]


def spec_with(
    *,
    body: Json | None = None,
    response: Json | None = None,
    params: list[Json] | None = None,
    extra_paths: Json | None = None,
) -> Json:
    op: Json = {
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": response
                        or {
                            "type": "object",
                            "properties": {"id": {"type": "string"}, "n": {"type": "integer"}},
                            "required": ["id"],
                        }
                    }
                }
            }
        }
    }
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    if params is not None:
        op["parameters"] = params
    paths: Json = {"/v1/things": {"post": op, "get": {"responses": {"200": {"description": "ok"}}}}}
    paths.update(extra_paths or {})
    return {"openapi": "3.1.0", "paths": paths}


BODY = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "size": {"type": "integer"}},
    "required": ["name"],
}


def test_committed_snapshot_is_current_and_has_no_unacknowledged_breaking_change() -> None:
    unacked, stale = oc.check(app.openapi())
    assert not unacked, (
        "Breaking API changes vs docs/openapi.json (a vendor would be broken by these):\n  - "
        + "\n  - ".join(unacked)
        + "\nIf intended, add the exact strings to docs/openapi-breaking-ack.json and run `make openapi`."
    )
    assert not stale, "docs/openapi.json is out of date: run `make openapi` and commit the result."


def test_identical_specs_have_no_breaking_changes() -> None:
    s = spec_with(body=BODY)
    assert oc.breaking_changes(s, copy.deepcopy(s)) == []


def test_additions_are_not_breaking() -> None:
    old = spec_with(body=BODY)
    new = copy.deepcopy(old)
    new["paths"]["/v1/other"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    new["paths"]["/v1/things"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["optional_new"] = {"type": "string"}
    new["paths"]["/v1/things"]["post"]["responses"]["200"]["content"]["application/json"]["schema"][
        "properties"
    ]["extra"] = {"type": "string"}
    assert oc.breaking_changes(old, new) == []


def test_removed_path_and_method_are_breaking() -> None:
    old = spec_with(body=BODY)
    no_path = copy.deepcopy(old)
    del no_path["paths"]["/v1/things"]
    assert oc.breaking_changes(old, no_path) == ["path /v1/things was removed"]
    no_method = copy.deepcopy(old)
    del no_method["paths"]["/v1/things"]["get"]
    assert oc.breaking_changes(old, no_method) == ["GET /v1/things was removed"]


def test_removed_or_newly_required_request_field_is_breaking() -> None:
    old = spec_with(body=BODY)
    removed = copy.deepcopy(old)
    del removed["paths"]["/v1/things"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["properties"]["size"]
    assert oc.breaking_changes(old, removed) == ["POST /v1/things request field 'size' was removed"]

    required = copy.deepcopy(old)
    required["paths"]["/v1/things"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "required"
    ] = [
        "name",
        "size",
    ]
    assert oc.breaking_changes(old, required) == [
        "POST /v1/things request field 'size' is now required"
    ]

    brand_new = copy.deepcopy(old)
    schema = brand_new["paths"]["/v1/things"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    schema["properties"]["tenant"] = {"type": "string"}
    schema["required"] = ["name", "tenant"]
    assert oc.breaking_changes(old, brand_new) == [
        "POST /v1/things request has a new required field 'tenant'"
    ]


def test_response_field_removed_or_retyped_is_breaking() -> None:
    old = spec_with(body=BODY)
    removed = copy.deepcopy(old)
    del removed["paths"]["/v1/things"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["properties"]["n"]
    assert oc.breaking_changes(old, removed) == [
        "POST /v1/things response 200 field 'n' was removed"
    ]

    retyped = copy.deepcopy(old)
    retyped["paths"]["/v1/things"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["properties"]["n"] = {"type": "string"}
    assert oc.breaking_changes(old, retyped) == [
        "POST /v1/things response 200 field 'n' changed type integer -> string"
    ]


def test_nested_ref_changes_are_seen_through_components() -> None:
    def make(item_type: str) -> Json:
        s = spec_with(response={"$ref": "#/components/schemas/Page"})
        s["components"] = {
            "schemas": {
                "Page": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}
                    },
                },
                "Item": {"type": "object", "properties": {"score": {"type": item_type}}},
            }
        }
        return s

    assert oc.breaking_changes(make("number"), make("number")) == []
    assert oc.breaking_changes(make("number"), make("string")) == [
        "POST /v1/things response 200 field 'items' field 'score' changed type number -> string"
    ]


def test_parameters_removed_or_made_required_are_breaking() -> None:
    q = {"in": "query", "name": "limit", "required": False, "schema": {"type": "integer"}}
    old = spec_with(params=[q])
    removed = spec_with(params=[])
    assert oc.breaking_changes(old, removed) == [
        "POST /v1/things query parameter 'limit' was removed"
    ]
    required = spec_with(params=[{**q, "required": True}])
    assert oc.breaking_changes(old, required) == [
        "POST /v1/things query parameter 'limit' is now required"
    ]
    added_required = spec_with(
        params=[
            q,
            {"in": "query", "name": "tenant", "required": True, "schema": {"type": "string"}},
        ]
    )
    assert oc.breaking_changes(old, added_required) == [
        "POST /v1/things has a new required query parameter 'tenant'"
    ]


def test_removed_2xx_response_is_breaking() -> None:
    old = spec_with(body=BODY)
    new = copy.deepcopy(old)
    new["paths"]["/v1/things"]["post"]["responses"] = {"201": {"description": "created"}}
    assert "POST /v1/things response 200 was removed" in oc.breaking_changes(old, new)


def test_check_honours_the_ack_file_and_detects_a_stale_snapshot(tmp_path: Path) -> None:
    old = spec_with(body=BODY)
    snap = tmp_path / "openapi.json"
    ack = tmp_path / "ack.json"
    snap.write_text(oc.render(old))
    new = copy.deepcopy(old)
    del new["paths"]["/v1/things"]["get"]

    unacked, stale = oc.check(new, snap, ack)
    assert unacked == ["GET /v1/things was removed"] and stale

    ack.write_text(json.dumps(["GET /v1/things was removed"]))
    unacked, stale = oc.check(new, snap, ack)
    assert unacked == [] and stale  # acknowledged, but the snapshot still has to be regenerated

    snap.write_text(oc.render(new))
    assert oc.check(new, snap, ack) == ([], False)


def test_ack_file_must_be_a_list_of_strings(tmp_path: Path) -> None:
    bad = tmp_path / "ack.json"
    bad.write_text('{"x": 1}')
    with pytest.raises(ValueError):
        oc.load_ack(bad)


def test_every_operation_is_documented_under_v1_with_tag_and_summary() -> None:
    spec = app.openapi()
    assert spec["paths"], "no paths"
    for path, item in spec["paths"].items():
        assert path.startswith("/v1/"), f"{path} is not under /v1"
        for method, op in item.items():
            where = f"{method.upper()} {path}"
            assert op.get("tags"), f"{where} has no tag"
            assert op.get("summary"), f"{where} has no summary"
            assert {"401", "403", "500"} <= set(op["responses"]), (
                f"{where} lacks the error responses"
            )
    assert "/health" not in spec["paths"] and "/documents" not in spec["paths"]
    declared = {t["name"] for t in spec["tags"]}
    used = {t for item in spec["paths"].values() for op in item.values() for t in op["tags"]}
    assert used <= declared


def test_error_schema_is_published() -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert set(schemas["ErrorResponse"]["properties"]) == {"detail", "code", "request_id"}
    assert "limit" in {
        p["name"] for p in app.openapi()["paths"]["/v1/documents"]["get"]["parameters"]
    }
