"""OpenAPI snapshot + breaking-change detection (ticket 36).

``docs/openapi.json`` is the committed contract. A change is **breaking** for existing clients when it:

* removes a path or an HTTP method,
* removes a request parameter (query/path/header) or request-body field,
* makes a request parameter / body field newly required (or adds a new required one),
* removes a 2xx response, or removes a field from a 2xx JSON response body,
* changes the type of a response field (or a request field).

Each finding is a stable string. Intended breaks are acknowledged by listing those exact strings in
``docs/openapi-breaking-ack.json`` (a JSON list); the file doubles as the audit log of accepted breaks.

CLI (run from ``backend/``)::

    python -m app.openapi_contract export    # write docs/openapi.json (refuses unacknowledged breaks)
    python -m app.openapi_contract check     # exit 1 on unacknowledged breaks or a stale snapshot
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "docs" / "openapi.json"
ACK_FILE = REPO_ROOT / "docs" / "openapi-breaking-ack.json"
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

Json = dict[str, Any]


def render(spec: Json) -> str:
    """Canonical text of a spec (what is committed)."""
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _resolve(spec: Json, node: Any, seen: frozenset[str] = frozenset()) -> Any:
    """Inline ``$ref``s (cycle-safe: a cycle collapses to ``{"$cycle": name}``)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in seen:
                return {"$cycle": ref}
            target: Any = spec
            for part in ref[2:].split("/"):
                target = target[part]
            return _resolve(spec, target, seen | {ref})
        return {k: _resolve(spec, v, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(spec, v, seen) for v in node]
    return node


def _type_sig(schema: Any) -> str:
    """A comparable type signature: type/format/enum/items, with unions order-insensitive."""
    if not isinstance(schema, dict):
        return "any"
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return "|".join(sorted(_type_sig(s) for s in schema[key]))
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return _type_sig(schema["allOf"][0])
    if "$cycle" in schema:
        return "ref:" + str(schema["$cycle"])
    typ = schema.get("type", "object" if "properties" in schema else "any")
    if typ == "array":
        return f"array<{_type_sig(schema.get('items'))}>"
    fmt = schema.get("format")
    return f"{typ}:{fmt}" if fmt else str(typ)


def _fields(schema: Any) -> tuple[dict[str, Any], set[str]]:
    """(properties, required) of an object schema; merges ``allOf`` members; unwraps arrays."""
    if not isinstance(schema, dict):
        return {}, set()
    if schema.get("type") == "array":
        return _fields(schema.get("items"))
    props: dict[str, Any] = dict(schema.get("properties", {}))
    required: set[str] = set(schema.get("required", []))
    for part in schema.get("allOf", []):
        p, r = _fields(part)
        props.update(p)
        required |= r
    return props, required


def _diff_fields(
    where: str, old: Any, new: Any, *, request: bool, out: list[str], depth: int = 0
) -> None:
    """Compare two (resolved) schemas field by field, recursing into nested objects and arrays."""
    if depth > 6:
        return
    old_props, old_req = _fields(old)
    new_props, new_req = _fields(new)
    for name, old_schema in old_props.items():
        label = f"{where} field '{name}'"
        if name not in new_props:
            out.append(f"{label} was removed")
            continue
        if _type_sig(old_schema) != _type_sig(new_props[name]):
            out.append(
                f"{label} changed type {_type_sig(old_schema)} -> {_type_sig(new_props[name])}"
            )
            continue
        if request and name not in old_req and name in new_req:
            out.append(f"{label} is now required")
        _diff_fields(label, old_schema, new_props[name], request=request, out=out, depth=depth + 1)
    if request:
        for name in sorted(new_req - set(old_props)):
            out.append(f"{where} has a new required field '{name}'")


def _params(op: Json) -> dict[tuple[str, str], Json]:
    return {(p["in"], p["name"]): p for p in op.get("parameters", [])}


def _json_schema(content: Any) -> Any:
    if not isinstance(content, dict):
        return None
    for media in ("application/json", "multipart/form-data"):
        if media in content:
            return content[media].get("schema")
    return None


def breaking_changes(old: Json, new: Json) -> list[str]:
    """Breaking differences going from spec ``old`` to spec ``new`` (sorted, deterministic)."""
    out: list[str] = []
    old_paths: Json = old.get("paths", {})
    new_paths: Json = new.get("paths", {})
    for path, old_item in old_paths.items():
        if path not in new_paths:
            out.append(f"path {path} was removed")
            continue
        for method in _METHODS:
            if method not in old_item:
                continue
            op_id = f"{method.upper()} {path}"
            if method not in new_paths[path]:
                out.append(f"{op_id} was removed")
                continue
            _diff_operation(
                op_id, _resolve(old, old_item[method]), _resolve(new, new_paths[path][method]), out
            )
    return sorted(set(out))


def _diff_operation(op_id: str, old_op: Json, new_op: Json, out: list[str]) -> None:
    old_params, new_params = _params(old_op), _params(new_op)
    for key, old_p in old_params.items():
        label = f"{op_id} {key[0]} parameter '{key[1]}'"
        if key not in new_params:
            out.append(f"{label} was removed")
            continue
        new_p = new_params[key]
        if not old_p.get("required") and new_p.get("required"):
            out.append(f"{label} is now required")
        if _type_sig(old_p.get("schema")) != _type_sig(new_p.get("schema")):
            out.append(f"{label} changed type")
    for key, new_p in new_params.items():
        if key not in old_params and new_p.get("required"):
            out.append(f"{op_id} has a new required {key[0]} parameter '{key[1]}'")

    old_body = _json_schema(old_op.get("requestBody", {}).get("content"))
    new_body = _json_schema(new_op.get("requestBody", {}).get("content"))
    if old_body is not None:
        if new_body is None:
            out.append(f"{op_id} request body was removed")
        else:
            _diff_fields(f"{op_id} request", old_body, new_body, request=True, out=out)
    elif new_op.get("requestBody", {}).get("required"):
        out.append(f"{op_id} now requires a request body")

    old_resp: Json = old_op.get("responses", {})
    new_resp: Json = new_op.get("responses", {})
    for code, old_r in old_resp.items():
        if not str(code).startswith("2"):
            continue
        if code not in new_resp:
            out.append(f"{op_id} response {code} was removed")
            continue
        old_s = _json_schema(old_r.get("content"))
        new_s = _json_schema(new_resp[code].get("content"))
        if old_s is None:
            continue
        if new_s is None:
            out.append(f"{op_id} response {code} body was removed")
        elif _type_sig(old_s) != _type_sig(new_s):
            out.append(
                f"{op_id} response {code} changed type {_type_sig(old_s)} -> {_type_sig(new_s)}"
            )
        else:
            _diff_fields(f"{op_id} response {code}", old_s, new_s, request=False, out=out)


def load_ack(path: Path = ACK_FILE) -> set[str]:
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"{path} must be a JSON list of change strings")
    return set(data)


def current_spec() -> Json:
    from app.main import app

    return app.openapi()


def check(
    current: Json | None = None, snapshot: Path = SNAPSHOT, ack: Path = ACK_FILE
) -> tuple[list[str], bool]:
    """Return (unacknowledged breaking changes, snapshot is stale)."""
    spec = current if current is not None else current_spec()
    committed = json.loads(snapshot.read_text(encoding="utf-8"))
    unacked = [c for c in breaking_changes(committed, spec) if c not in load_ack(ack)]
    stale = snapshot.read_text(encoding="utf-8") != render(spec)
    return unacked, stale


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "export":
        spec = current_spec()
        if SNAPSHOT.is_file():
            unacked, _ = check(spec)
            if unacked:
                print("Refusing to export: unacknowledged breaking changes:", file=sys.stderr)
                for c in unacked:
                    print(f"  - {c}", file=sys.stderr)
                print(
                    f"Add the exact strings to {ACK_FILE.relative_to(REPO_ROOT)} to accept them.",
                    file=sys.stderr,
                )
                return 1
        SNAPSHOT.write_text(render(spec), encoding="utf-8")
        print(f"wrote {SNAPSHOT.relative_to(REPO_ROOT)}")
        return 0
    if cmd == "check":
        unacked, stale = check()
        for c in unacked:
            print(f"BREAKING: {c}", file=sys.stderr)
        if stale:
            print("docs/openapi.json is out of date: run `make openapi`.", file=sys.stderr)
        return 1 if unacked or stale else 0
    print("usage: python -m app.openapi_contract [export|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
