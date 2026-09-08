# AGENTS.md

This repo uses **CLAUDE.md** as its agent operating manual (project scope, action guards,
architecture, config loading, quality gates, commit conventions, and cost/risk gotchas).
Read it before doing anything.

- **Operating rules:** [`./CLAUDE.md`](./CLAUDE.md)
- **Founding context:** [`./context/v0/README.md`](./context/v0/README.md)
- **Stack decision:** [`./docs/adr/0001-stack.md`](./docs/adr/0001-stack.md)

Key rules in brief:

- **Never** `git commit` / `git push` / deploy without explicit instruction.
- Run `make check` (lint + typecheck + test) before any commit — fix all failures first.
- Backend is uv-managed: always go through `make` or `uv run` from `backend/`; never bare
  `pytest`/`ruff`/`mypy`.
- Never commit document content — raw uploads live only under `data/` / `tmp/`.
- Surface design decisions with tradeoffs rather than picking silently.