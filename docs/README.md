# Documentation index

| File | What it is | Edit by hand? |
|---|---|---|
| [architecture.md](architecture.md) | System diagram, deployment matrix, design boundaries | yes |
| [api.md](api.md) | Using the HTTP API: auth, API keys, search, chat, streaming (SSE), cache headers, `/v1`, examples | yes |
| [configuration.md](configuration.md) | Every setting, defaults, scope (deployment vs per-request), tuning notes | yes |
| [performance.md](performance.md) | Query-cache benchmark: method, numbers, caveats, how to quote them | yes (numbers come from the JSON below) |
| [deployment.md](deployment.md) | Source vs published images, tags, upgrading, Kubernetes image overrides, release checklist | yes |
| [../CHANGELOG.md](../CHANGELOG.md) | Release notes (Keep a Changelog) | yes |
| [kubernetes-local.md](kubernetes-local.md) | Running the manifests on kind | yes |
| [adr/](adr/) | Decisions and rejected alternatives: stack, parsing, chunking, cache, `/v1` contract, SSE streaming, API keys, runtime settings (0001-0008) | append-only |
| [design/incremental-document-update.md](design/incremental-document-update.md) | Design record behind ADR-0003 (content-defined chunking); shipped in ticket 15 | no (historical) |
| [design/evals-brainstorm.md](design/evals-brainstorm.md) | Ideas only for an evals framework (ticket 27, open); nothing is built | yes (when ticket 27 starts) |

## Project files at the repository root

| File | What it is |
|---|---|
| [../README.md](../README.md) | Project overview and quickstart |
| [../AGENTS.md](../AGENTS.md) | Contributor and coding-agent guide: architecture map, conventions, config layering, test rules (`CLAUDE.md` imports it) |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Dev setup, branch and commit conventions, adding a route with tests, PR checklist |
| [../SECURITY.md](../SECURITY.md) | How to report a vulnerability privately, scope, supported versions |
| [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | Contributor Covenant 2.1 |
| [../LICENSE](../LICENSE) | MIT |
| [../.env.example](../.env.example) | Minimal environment for a bare-bones install (secrets, bootstrap admin, infra URLs) |
| [../.env.reference](../.env.reference) | Every environment tunable, commented; the same keys are editable in Admin > Settings |

## The JSON files (generated, not prose)

- **`openapi.json`** is the committed API contract, exported from the running app (`make openapi`). A test fails if the live API drifts from it, so
  accidental breaking changes cannot ship. Never edit it by hand.
- **`openapi-breaking-ack.json`** is the list of *deliberately accepted* breaking API changes. It is `[]` on purpose: empty means no breaking change has been
  approved. An entry is added only in a change that intends to break the contract (see the "Contract" section of [api.md](api.md)).
- **`performance-cache-bench.json`** is the raw output of `scripts/bench_cache.py` behind the tables in `performance.md`, kept so the numbers can be audited and re-derived.
