# Evals for the RAG framework — brainstorm (not a decision)

Status: **Brainstorm, not a decision** (ticket 27 is open) — ideas only, captured 2026-11-14 for a later ticket (27). Nothing here is built. The only
"eval" today is an ad-hoc script (`tmp/smoke_tests/search-real-world-smoke.py`, 14 hand-labelled
questions over two real Fannie Mae chapters) — it is the seed of the golden-set idea below.

## 1. What to measure

| Layer | Question it answers | Candidate metrics |
|---|---|---|
| Parse | Did we read the document correctly? | CER/WER vs ground-truth text per provider (OCR vs vision LLM vs native text); table/heading preservation |
| Chunk | Do edits leave chunks alone? | % chunk hashes shared across versions (CDC vs fixed-size — ticket 21), embedded-per-edit, chunk size distribution |
| Retrieve | Is the answer-bearing passage returned? | hit@k, recall@k, MRR, nDCG; per mode (semantic/keyword/hybrid), per index/metric, per embedder |
| Generate | Is the answer right and supported? | answer-fact match, faithfulness (claims supported by cited chunks), answer relevance, citation precision/recall |
| Abstain | Does it say "not in the documents" when it should — and only then? | abstention precision/recall on unanswerable + near-miss questions |
| System | What does it cost? | latency p50/p95, tokens, embeddings called, $ per question |

## 2. Datasets (the hard part)

- **Hand-labelled golden set per corpus**: question, expected facts (alternatives allowed), the
  passage that must be retrieved, question kind (paraphrase / id-lookup / numeric / multi-fact /
  colloquial / unanswerable). Stored as versioned JSONL; the labelling script/URLs live in the
  repo, **the documents do not** (licensing + the repo's "synthetic fixtures only" rule) — fetch-on-demand
  with checksums.
- **Synthetic expansion**: have an LLM write questions *from* a chunk (the chunk is the label), then
  paraphrase them; filter by a second model. Cheap breadth, but keep it separate from the hand set
  (it over-represents what the generating model finds easy).
- **Versioned-document questions**: where v1 and v2 of a document disagree, the right answer depends
  on which version is indexed. Directly tests incremental update (stale facts must disappear).
- **Adversarial**: near-miss (right topic, wrong detail), contradictory chunks, questions that need
  two chunks, table lookups, OCR-noisy text, prompt injection inside a document.
- **Production mining**: dashboard thumbs-up/down + "this citation is wrong" -> candidate cases,
  human-reviewed before they enter the golden set.
- Hold-out split from day one so tuning chunk size/top-k does not overfit the questions we look at.

## 3. Scoring

- Deterministic first: normalised substring/regex for facts, source-in-top-k. Fast, free, CI-safe.
  Known weakness: OCR noise breaks exact matches -> normalise (case, dashes, whitespace) and report
  near-misses instead of loosening until it passes.
- LLM-as-judge only where deterministic fails (faithfulness, open-ended correctness): rubric prompt,
  temperature 0, a *different* model family than the generator (self-preference bias), judge sees the
  question + cited chunks + answer. Calibrate against ~50 human labels and publish agreement; a judge
  we have not calibrated is a vibes generator.
- Small-n honesty: 14 questions cannot show a 5-point improvement. Report bootstrap confidence
  intervals; use paired comparisons between configs on the same questions; state the minimum
  detectable effect. Aim for 100+ per corpus before trusting deltas.
- Pin everything that can drift: model ids, prompts (hash them), embedder, chunker fingerprint,
  index params, top-k; store them with each run.

## 4. Shape of the framework

- `backend/app/evals/` (or a separate package): dataset schema + loader, `Runner` (black-box over
  the HTTP API so it exercises auth, retrieval, LLM exactly as users do; optionally in-process for
  retrieval-only runs), metric plug-ins, report writer.
- **Config sweeps** are the payoff: matrix over retrieval mode x fusion x top_k x chunk size x
  embedder x vector index/metric (tickets 23/24 create exactly these knobs). Output: a table of
  quality vs latency vs cost, so "which hybrid weights?" becomes a measured answer.
- **Storage**: Postgres tables (`eval_runs`, `eval_cases`, `eval_results`) so the dashboard can show
  runs, diff two runs per question, and highlight regressions; mirror to **Langfuse** datasets /
  experiments + scores (we already trace there) so each result links to its trace.
- **Surfaces**: `make eval` (local), a dashboard page (runs, per-question diff, metric charts),
  permissions `evals:read` / `evals:run` in the RBAC catalogue.
- **CI**: cheap deterministic subset on every PR with thresholds (fail on retrieval hit@k drop);
  LLM-judged suite nightly/manual because of cost and nondeterminism.

## 5. Pitfalls to design around

- Free-tier LLMs are rate-limited and nondeterministic -> retries, caching of judge calls, record raw
  outputs, never silently drop a question.
- Evaluating on data we tuned on. Leakage between synthetic questions and the chunks used to tune.
- Optimising a metric that does not match users (hit@k high but answers wrong, or vice versa) —
  always report retrieval and answer metrics side by side (as the current script does).
- A "refuse more" model looks great on faithfulness. Pair faithfulness with answer coverage.
- Corpus rights: never commit third-party documents; commit labels + fetch recipes.

## 6. Phasing sketch

0. Promote the ad-hoc script into a runner + JSONL schema; deterministic retrieval + fact metrics.
1. Abstention and versioned-document cases; per-kind breakdown; confidence intervals.
2. LLM-judge faithfulness/relevance with a calibrated human-labelled slice.
3. Sweeps over tickets 23/24 knobs; result storage; dashboard page; Langfuse datasets.
4. CI gating and production-feedback loop.

## 7. Questions for the owner

- Judge model: free OpenRouter models (rate limits, weaker) or a paid model for the judge only?
- Which corpus is the public benchmark? (Selling Guide excerpts cannot be committed; options:
  fetch-on-demand, or a public-domain/openly-licensed corpus we can ship.)
- Where should results live — Postgres + dashboard, Langfuse only, or both?
- How strict should CI gating be, given nondeterministic generation?
