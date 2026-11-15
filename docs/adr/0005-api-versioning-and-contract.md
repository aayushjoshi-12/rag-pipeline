# 0005 — API versioning and contract

- Status: **Accepted**
- Date: 2026-11-15 (ticket 36)

## Context

Other teams may build on the API, so it has to be stable, discoverable and protected against accidental breaking changes, without breaking the dashboard.

## Decision

- **Everything is served under `/v1`**; those are the only paths in OpenAPI. The unversioned paths remain as **deprecated aliases** (hidden from OpenAPI, `Deprecation: true` and a `Link: rel="successor-version"` header). The dashboard keeps calling the aliases, so it needed no change. `/health` stays unversioned for probes.
- **Additive error envelope** `{detail, code, request_id}`: `detail` keeps its old shape so existing clients and the dashboard are unaffected; `code` is a stable machine-readable name; `X-Request-ID` is echoed (sanitised), logged and bound to the log context.
- **Pagination is versioned, not changed:** `/v1/documents` returns `{items,total,limit,offset,has_more}`; the legacy alias keeps its bare array.
- **`docs/openapi.json` is the committed contract.** A test fails on a stale snapshot or on an unacknowledged breaking change (removed path/method/param/field, newly required, removed 2xx, changed response type). `make openapi` regenerates it and refuses while a break is unacknowledged; accepted breaks are listed in `docs/openapi-breaking-ack.json` (empty = none approved).
- `GET /v1/capabilities` (credential required, no secrets); `CORS_ALLOW_ORIGINS` default **none** (no middleware at all), never credentials, preflight answered before auth.
- **Headless deployment:** the dashboard is the Compose `ui` profile (`make up` unchanged in effect, `make up-api` without it) and a K8s `headless` overlay.

## Rejected

- **Moving the dashboard to `/v1` now** — risk with no gain; aliases cost nothing.
- **A nested `{error:{...}}` envelope** — breaks every client that reads `detail`.
- **Diffing the contract against git history in tests** — fragile in CI/worktrees; the snapshot plus the explicit ack file is reviewable in one diff.

## Consequences

- A hand edit of `openapi.json` bypasses the protection; reviewers must read that diff (documented in `docs/api.md`).
- Error bodies are no longer byte-identical across calls (`request_id`); security tests that compare bodies strip it.
- Not included: per-key rate limiting, removing the aliases, SDK generation, paginating the small admin lists.
