# 0008 - Runtime settings: an admin UI layer

- Status: **Accepted** (owner decisions 2026-11-15)
- Date: 2026-11-15 (ticket 43)

## Context

Everything was configured through env vars / `config.<env>.json`; changing a model, a TTL or a provider key
meant editing files and restarting. A deployment that starts bare-bones should be able to switch services and tune
parameters later from the UI.

## Decision

1. **Precedence: env var (incl. `.env`) > UI (database) > config.json > built-in default.** A key pinned by the
   environment is shown locked with the variable name and rejects writes (409). Implemented as one more
   pydantic-settings source ranked between the env/dotenv sources and the JSON source, so *every* `Settings()`
   (API, Celery, CLI) includes the layer without call-site changes.
2. **Storage.** `app_settings(key, value JSON | secret_blob, is_secret, updated_at, updated_by)` and
   `settings_audit`. Removing a row returns the key to config.json / default (the UI "reset").
3. **Freshness without a push channel.** Each process holds a snapshot. API: refreshed at most every
   `SETTINGS_REFRESH_SECONDS` (5 s) on the request path and immediately after its own write. Celery: refreshed at
   the start of each task. The database stays the single source of truth; replicas converge within the interval.
   A snapshot is installed only after a full `Settings()` validates from it; otherwise the last good one stays and the
   error is surfaced. A missing table or an unreachable DB never fails a request or task.
4. **Registry.** Every `Settings` field is classified (test-enforced): `live`, `restart`, `reindex`, `bootstrap`
   (read-only, with the reason) and `secret`. Types, defaults and validation come from the pydantic model itself;
   the registry adds only group, description, apply mode and notes. Import-time captures of editable keys were
   removed (embeddings, vector store, parser backends, health).
5. **Reindex-class keys** require `confirm_reindex=true`; the value is applied, a Celery `reindex_vectors` (or
   `rebuild_fts`) task is queued, and the page shows its state plus the existing drift report. Until it finishes the
   app refuses to search a mismatched index (existing behaviour), which the confirm text states.
6. **Secrets: write-only, encrypted.** Fernet with a key derived (HKDF-SHA256) from `SETTINGS_ENCRYPTION_KEY`
   (env / K8s Secret). The plaintext is an envelope `{k: setting, v: value}` so a ciphertext moved to another row
   fails to decrypt. No endpoint ever returns a secret; the UI shows set / not set, who and when. Rotation:
   `SETTINGS_ENCRYPTION_KEY_PREVIOUS` + `python -m app.manage rotate-settings-key`. Without the key, secret fields
   are disabled and other settings still work. Rows that cannot be decrypted are reported and not applied.
7. **Permissions.** `settings:read` (view, no secret values) and `settings:manage` (write, test, reindex, audit),
   admin only; `settings:manage` is in `KEY_FORBIDDEN` (API keys never change configuration).
8. **Audit.** Every change records actor, action, key and old/new value; secrets record the action only.
9. **Guided flows.** The UI groups provider-switch fields (LLM, embeddings, vector store, storage, parser), shows the
   consequence and offers a cheap, safe "Test connection" per service.

## Not editable from the UI (bootstrap)

`DATABASE_URL`, `REDIS_URL`, `CELERY_*` (needed before the DB/broker can be read), `SECRET_KEY`, `AUTH_ENABLED`,
`ADMIN_*` (lock-out / session re-keying), `URL_INGEST_ALLOW_PRIVATE` (disables the SSRF guard), `TESSERACT_CMD`
(a program the server executes), `DATA_DIR`, `PORT`, `RAG_ENV`, `SETTINGS_ENCRYPTION_KEY*`, `SETTINGS_REFRESH_SECONDS`.
Chosen as the safest option; relaxing any of them is a deliberate follow-up.

## Threat model

- DB dump alone: secrets are ciphertext; the key lives outside the database. Key alone: useless without the DB.
- App compromise reveals in-memory secrets (inherent to any app that uses them).
- `settings:manage` is highly privileged: an admin can repoint a `*_BASE_URL` / endpoint and thereby receive the
  stored key. Hence admin-only by default, forbidden to API keys, audited, and noted in the UI.
- Secrets never appear in responses, logs (only keys are logged), traces, audit rows, error messages (validation
  errors never echo input) or OpenAPI examples; tests grep for a canary value.
- With `AUTH_ENABLED=false` every request is an admin (existing dev/demo behaviour), so the page is open too.
- CSRF does not apply (bearer tokens). A stale replica serves old values for at most the refresh interval.

## Rejected / not built

- **Redis pub/sub invalidation**: another moving part; the polling bound is acceptable.
- **Storing secrets in plain text, or hashing them**: the app needs the value.
- **A secret read-back / reveal endpoint**: replace or clear only.
- **Letting the UI override env vars**: rejected by the owner's precedence.
- **Per-document reindex progress, four-eyes approval, live `LOG_LEVEL`/Langfuse keys**: not included.

## Consequences

- To manage a key from the UI it must not be set in the environment (Compose passes all of `.env`; an empty
  variable still pins).
- A new `Settings` field fails the registry test until classified.
- New dependency: `cryptography` (Fernet).
