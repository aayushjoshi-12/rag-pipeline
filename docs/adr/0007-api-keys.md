# 0007 — API keys for machine clients

- Status: **Accepted**
- Date: 2026-11-15 (ticket 35)

## Context

Non-interactive consumers need credentials; email + password gives 30-minute JWTs meant for humans.

## Decision

- Keys look like `rag_<8-char prefix>_<256-bit urlsafe secret>`. Only the **SHA-256 hash** is stored; the secret is shown once at creation; comparison is constant-time.
- A key is **bound to an existing role**: permissions are re-read from the database on every request, so role edits and revocation apply immediately (as for users). You can only bind a role whose permissions you hold; binding `admin` is allowed but warned.
- Accepted as `X-API-Key` or `Authorization: Bearer`. If `X-API-Key` is present it is authoritative (a good `Authorization` header cannot rescue a bad key, a JWT in `X-API-Key` is rejected).
- Managed with the new `apikeys:manage` permission (admin only by default): create, list (never secrets), revoke (idempotent, row kept for audit). Traces show the caller as `key:<name>`. `last_used_at` is written at most once a minute per key.
- Requires `AUTH_ENABLED=true` (a key needs an owning user).

## Rejected / not built

- **Per-key rate limiting** — not included; do it at the gateway/ingress (e.g. by the `X-API-Key` header).
- **JWTs as long-lived service tokens** — cannot be revoked individually without a denylist.
- **Plain-text or reversibly encrypted secrets** — nothing to recover; a lost key is replaced.

## Consequences

- Revocation is immediate but a valid key can call as fast as the service allows.
- The `last_used_at` field means "used within the last minute", not an access log.
