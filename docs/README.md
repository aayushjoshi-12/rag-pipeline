# Documentation index

| File | What it is | Edit by hand? |
|---|---|---|
| [architecture.md](architecture.md) | System diagram, deployment matrix, design boundaries | yes |
| [api.md](api.md) | Using the HTTP API: auth, API keys, search, chat, streaming (SSE), cache headers, `/v1`, examples | yes |
| [configuration.md](configuration.md) | Every setting, defaults, scope (deployment vs per-request), tuning notes | yes |
| [performance.md](performance.md) | Query-cache benchmark: method, numbers, caveats, how to quote them | yes (numbers come from the JSON below) |
| [kubernetes-local.md](kubernetes-local.md) | Running the manifests on kind | yes |
| [adr/](adr/) | Decisions and rejected alternatives: stack, parsing, chunking | append-only |
| [design/](design/) | Working notes: incremental update, evals brainstorm | yes |

## The JSON files (generated, not prose)

- **`openapi.json`** is the committed API contract, exported from the running app (`make openapi`). A test fails if the live API drifts from it, so
  accidental breaking changes cannot ship. Never edit it by hand.
- **`openapi-breaking-ack.json`** is the list of *deliberately accepted* breaking API changes. It is `[]` on purpose: empty means no breaking change has been
  approved. An entry is added only in a change that intends to break the contract (see the "Contract" section of [api.md](api.md)).
- **`performance-cache-bench.json`** is the raw output of `scripts/bench_cache.py` behind the tables in `performance.md`, kept so the numbers can be audited and re-derived.
