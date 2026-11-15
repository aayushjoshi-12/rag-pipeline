# Contributing to rag-pipeline

Thanks for helping. This guide covers setup, conventions and what a good pull request looks like.
Coding agents should read [AGENTS.md](AGENTS.md) as well; it holds the architecture map and rules.

By contributing you agree your work is released under the [MIT License](LICENSE).

## Development setup

Prerequisites: Docker with Compose, [uv](https://docs.astral.sh/uv/) (Python 3.12+), Node 20+ and npm.

```bash
git clone <your-fork-url> && cd rag-pipeline
make install                     # backend (uv sync) + frontend (npm install)
cp .env.example .env             # fill SECRET_KEY, SETTINGS_ENCRYPTION_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
make up                          # full stack in Docker Compose
```

`make help` lists every target. Providers, models and retrieval options are set in Admin > Settings after
you log in; `.env.reference` lists every tunable if you prefer environment variables. Provider keys
(OpenRouter, OpenAI, Anthropic) are yours; the test suite never needs them.

For host-run development: `make db-up`, `make migrate`, then `make run` (API on :8000, dashboard on :5173).

## Checks

```bash
make check     # ruff + mypy (strict) + pytest (backend) and eslint/prettier + tsc + vitest (frontend)
make fmt       # apply formatting
```

Run `make check` before opening a pull request; CI runs the same gates. Always go through `make` or
`uv run` (from `backend/`); a bare `pytest`, `ruff` or `mypy` may resolve to a global install without the
project's dependencies. `make check` starts a throwaway Chroma on :8002 (and removes it); a bare
`make backend-test` needs `make chroma-test-up` first. Tests never touch your dev Chroma or database.

## Branches and commits

- Branch names: `feat/<short>`, `fix/<short>`, `chore/<short>`, `docs/<short>`, `test/<short>`, `refactor/<short>`.
- [Conventional Commits](https://www.conventionalcommits.org/) titles: `feat(api): add X`, `fix(worker): handle Y`,
  `docs(readme): clarify Z`. Keep commits focused; add new commits instead of rewriting shared history.

## Adding a route or a service

1. Put business logic in `backend/app/services/` (no HTTP knowledge) and the handler in `backend/app/routers/`
   (one file per resource; request/response models in `backend/app/schema/`).
2. Add a **unit test** in `backend/tests/unit/` (pure logic, fakes) and an **integration test** in
   `backend/tests/integration/` covering the happy path and the main error path.
3. If the API contract changes, run `make openapi` and commit `docs/openapi.json`. A test fails on drift; an
   intentional breaking change must be acknowledged in `docs/openapi-breaking-ack.json` (see `docs/api.md`).
4. A new `Settings` field must be classified in `backend/app/services/settings_registry.py` (a test enforces it)
   and documented in `docs/configuration.md`.
5. Frontend changes: add or update Vitest tests next to the component or page.

## Testing rules

- No real API calls in tests (no live OpenAI, Anthropic or OpenRouter). Use fakes, `respx`/`pytest-httpx` or `MagicMock`.
- Integration tests use a real throwaway Postgres, never SQLite and never your dev database.
- Test fixtures must be **synthetic**. Never commit real document content, customer data, credentials or `.env` files.
- Suites run with `CACHE_ENABLED=false`; cache tests opt in on a dedicated Redis db.
- Aim for 70% coverage on new code.

## Coding style

- Python 3.12+, `from __future__ import annotations`, full type annotations (mypy is strict), pydantic models for API shapes, ruff for lint and format.
- Do not add `# noqa` or `# type: ignore` without a one-line reason.
- TypeScript is strict; eslint and prettier are enforced by `make check`.
- Keep changes scoped. Unrelated cleanups belong in a separate pull request.

## Pull request checklist

- [ ] `make check` passes locally
- [ ] Tests added or updated (unit and integration for backend changes)
- [ ] Docs updated (`docs/`, `README.md`, `.env.reference` if a setting changed; `docs/openapi.json` if the API changed)
- [ ] No secrets, personal data or real document content in the diff
- [ ] Conventional Commit title; description says what changed and why

## Reporting problems

Bugs and feature ideas: open an issue using the templates. Security issues: do **not** open a public issue;
follow [SECURITY.md](SECURITY.md). Be kind: see the [Code of Conduct](CODE_OF_CONDUCT.md).
