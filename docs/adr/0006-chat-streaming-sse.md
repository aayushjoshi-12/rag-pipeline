# 0006 — Chat streaming over Server-Sent Events

- Status: **Accepted**
- Date: 2026-11-15

## Context

`/chat` waits for the whole LLM completion; users stare at nothing for seconds. Sources are known long before the answer.

## Decision

`POST /chat/stream` (same body and `chat:use` permission as `/chat`) returns `text/event-stream`: `sources`, zero or more `token`, then exactly one `done` (answer, usage, `cost_usd`, cache verdict, `trace_id`) or `error`.

- **Everything that can fail cheaply runs before the response starts** (auth, validation, retrieval, LLM configuration, answer-cache lookup), so those are plain JSON errors and `X-Cache` is a real header. Only LLM failures mid-stream become an `error` event (HTTP 200 is already sent).
- **Cache:** a hit replays the stored answer as token events; a clean miss stores it; failure, client disconnect and bypass store nothing.
- **Cancellation:** a client disconnect closes the upstream LLM stream (thread bridge with a stop flag; the OpenAI-compatible stream yields empty liveness ticks during silent reasoning so a cancel takes effect). The trace is still written, as `error` / `client_disconnected`.
- One trace row and one generation per stream (`streamed`, `ttft_ms`); usage and cost come from the provider's final chunk (OpenRouter `usage.include`).
- **Frontend uses `fetch` + `ReadableStream`**, not `EventSource` (which cannot send an `Authorization` header), with the 401-refresh path and a fallback to `/chat` when the stream cannot be opened.

## Rejected

- **WebSockets** — bidirectional machinery we do not need; SSE works through ordinary HTTP infrastructure and curl.
- **Replacing `/chat`** — non-streaming clients and the answer cache semantics stay exactly as they were.
- **Coalescing concurrent identical streams** — unlike `/chat`, streams do not single-flight (documented).

## Consequences

- With a reasoning model the first token arrives late (measured ~4.5-5 s, ~89% of total time on the free nemotron model): streaming then mostly buys early sources and live rendering. A non-reasoning model gains much more.
- Not verified: Anthropic streaming against a live key (recorded fixtures only), provider-side billing after a cancel, behaviour behind a buffering proxy (`X-Accel-Buffering: no` is set).
