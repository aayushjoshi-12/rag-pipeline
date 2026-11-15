#!/usr/bin/env python3
"""Reproducible latency benchmark for the query cache (ticket 37).  Run against a real running stack:

    BENCH_EMAIL=... BENCH_PASSWORD=... python scripts/bench_cache.py [--reps 5] [--requests 60] [--chat-cold 12]

* Corpus: whatever is ingested in the target stack (the script prints documents/chunks; ingest the real PDFs first).
* Workload: the labelled questions in ``--questions`` (default tmp/smoke_tests/search-questions.json, refusal items
  skipped), drawn with a Zipf(s) repeat distribution; half of the repeats carry surface noise (extra spaces / case kept)
  that the cache normalises. NO paraphrases are replayed: a paraphrase is a different query to the cache by design.
* Conditions: cache OFF = every request sent with ``"cache": false`` (bypass: no read, no write); cache ON = default,
  starting cold (``POST /cache/purge`` before each run), so cold misses are INCLUDED in the ON numbers.
  OFF and ON runs are interleaved (OFF, ON, OFF, ON, ...) to spread drift (Ollama/CPU/thermal) over both.
* Also: an all-unique-queries run (the miss-path overhead), and /chat: ``--chat-cold`` real LLM calls (one per question,
  the miss path), then warm answer hits over ``--requests`` requests. LLM calls made are COUNTED and printed.
Prints a JSON document and a markdown summary. Never prints credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))]


def zipf_sample(n_items: int, n: int, s: float, rng: random.Random) -> list[int]:
    weights = [1 / (i + 1) ** s for i in range(n_items)]
    return rng.choices(range(n_items), weights=weights, k=n)


async def timed(client: httpx.AsyncClient, path: str, body: dict, sem: asyncio.Semaphore, out: list) -> None:
    async with sem:
        t0 = time.perf_counter()
        r = await client.post(path, json=body)
        ms = (time.perf_counter() - t0) * 1000
    out.append((ms, r.status_code, r.headers.get("x-cache", "?")))


async def run_batch(client, path, bodies, concurrency):
    sem = asyncio.Semaphore(concurrency)
    out: list = []
    t0 = time.perf_counter()
    await asyncio.gather(*[timed(client, path, b, sem, out) for b in bodies])
    wall = time.perf_counter() - t0
    ok = [ms for ms, code, _ in out if code == 200]
    errors = len(out) - len(ok)
    hits = sum(1 for _, code, x in out if code == 200 and x == "hit")
    return {
        "n": len(out), "errors": errors, "p50": pct(ok, 50), "p95": pct(ok, 95), "p99": pct(ok, 99),
        "rps": len(ok) / wall, "hit_rate": hits / max(1, len(ok)),
    }


def med(runs, key):
    xs = [r[key] for r in runs]
    return {"median": statistics.median(xs), "min": min(xs), "max": max(xs)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BASE", "http://localhost:8000"))
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--zipf", type=float, default=1.1)
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 8])
    ap.add_argument("--modes", nargs="+", default=["semantic", "keyword", "hybrid"])
    ap.add_argument("--chat-cold", type=int, default=12, help="real LLM calls (0 = skip /chat)")
    ap.add_argument("--questions", default=str(ROOT / "tmp/smoke_tests/search-questions.json"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    qs = [q["q"] for q in json.loads(Path(a.questions).read_text()) if not q.get("refusal")]

    async with httpx.AsyncClient(base_url=a.base, timeout=180) as c:
        tok = (await c.post("/auth/login", json={"email": os.environ["BENCH_EMAIL"], "password": os.environ["BENCH_PASSWORD"]})).json()["access_token"]
        c.headers["Authorization"] = f"Bearer {tok}"
        docs = (await c.get("/documents")).json()
        docs = docs if isinstance(docs, list) else docs.get("items", docs.get("documents", []))
        stats0 = (await c.get("/cache/stats")).json()
        env = {
            "machine": f"{platform.machine()} {platform.system()} {platform.release()}", "python": platform.python_version(),
            "documents": len(docs), "questions": len(qs), "cache_config": stats0["config"], "zipf_s": a.zipf,
            "requests_per_run": a.requests, "reps": a.reps, "concurrency": a.concurrency, "seed": a.seed,
        }

        def workload(mode: str, off: bool, unique: bool, offset: int):
            idx = zipf_sample(len(qs), a.requests, a.zipf, random.Random(a.seed + offset))
            bodies = []
            for j, i in enumerate(idx):
                q = qs[i]
                if unique:
                    q = f"{q} (variant {offset}-{j})"
                elif j % 2:
                    q = f"  {q}  "  # surface noise the cache normalises away
                bodies.append({"query": q, "mode": mode, "top_k": 5, **({"cache": False} if off else {})})
            return bodies

        results: dict = {"env": env, "search": {}}
        for mode in a.modes:
            for conc in a.concurrency:
                for unique in (False, True):
                    off_runs, on_runs = [], []
                    for rep in range(a.reps):
                        off_runs.append(await run_batch(c, "/search", workload(mode, True, unique, rep), conc))
                        await c.post("/cache/purge")
                        on_runs.append(await run_batch(c, "/search", workload(mode, False, unique, rep), conc))
                    key = f"{mode}|c{conc}|{'unique' if unique else 'zipf'}"
                    results["search"][key] = {
                        "off": {k: med(off_runs, k) for k in ("p50", "p95", "p99", "rps")},
                        "on": {k: med(on_runs, k) for k in ("p50", "p95", "p99", "rps", "hit_rate")},
                        "errors": sum(r["errors"] for r in off_runs + on_runs),
                    }
                    print("done", key, flush=True)

        if a.chat_cold > 0:
            await c.post("/cache/purge")
            cold, calls = [], 0
            for q in qs[: a.chat_cold]:
                t0 = time.perf_counter()
                r = await c.post("/chat", json={"question": q, "top_k": 5})
                ms = (time.perf_counter() - t0) * 1000
                calls += r.status_code == 200 and r.headers.get("x-cache") == "miss"
                cold.append((ms, r.status_code, r.headers.get("x-cache", "?")))
            ok_cold = [ms for ms, code, _ in cold if code == 200]
            warm_bodies = [{"question": qs[i], "top_k": 5} for i in zipf_sample(min(len(qs), a.chat_cold), a.requests * 3, a.zipf, rng)]
            warm = await run_batch(c, "/chat", warm_bodies, 8)
            results["chat"] = {
                "llm_calls_made": calls, "cold_errors": len(cold) - len(ok_cold),
                "cold_p50": pct(ok_cold, 50) if ok_cold else None, "cold_max": max(ok_cold) if ok_cold else None,
                "warm": warm,
            }
        results["cache_stats_after"] = (await c.get("/cache/stats")).json()["traces"]

    text = json.dumps(results, indent=2, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    print("\n| config | OFF p50 ms | ON p50 ms | OFF p95 | ON p95 | OFF rps | ON rps | ON hit rate |\n|---|---|---|---|---|---|---|---|")
    for k, v in results["search"].items():
        o, n = v["off"], v["on"]
        print(f"| {k} | {o['p50']['median']:.1f} | {n['p50']['median']:.1f} | {o['p95']['median']:.1f} | {n['p95']['median']:.1f} | "
              f"{o['rps']['median']:.1f} | {n['rps']['median']:.1f} | {n['hit_rate']['median']:.0%} |")


if __name__ == "__main__":
    asyncio.run(main())
