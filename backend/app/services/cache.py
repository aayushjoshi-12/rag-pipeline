"""Layered Redis query cache (ticket 37). See docs/configuration.md "Query cache" and docs/performance.md.

Three independent layers share one Redis db and the ``rag:cache:v1:`` prefix:

* **L1 embedding** - query text -> vector (saves the Ollama round trip).
* **L2 retrieval** - the ranked hits + neighbour context of the shared retrieval path. The key embeds the
  *index version*, so any mutation of the index makes every older entry unreachable at once.
* **L3 answer** - the LLM answer, keyed by the hash of the exact prompt (question + retrieved context), so an
  answer is never served for a different context.

Rules every layer follows: fail-open (Redis down or slow => the request runs uncached, never fails; a circuit
breaker keeps an outage from costing a timeout per request), nothing is cached unless the computation
succeeded, values above ``CACHE_MAX_VALUE_BYTES`` are skipped, and keys never contain user identity (the
corpus is shared; authorization runs in the route dependency *before* any lookup).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import time
import unicodedata
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import redis
import redis.asyncio as aioredis

from app.config import Settings

logger = logging.getLogger(__name__)

PREFIX = "rag:cache:v1:"
VERSION_KEY = PREFIX + "index_version"
BUMPS_KEY = PREFIX + "index_bumps"
LAYERS = ("embedding", "retrieval", "answer")
#: Bump when the cached payload formats or the key recipes change (old entries are then simply never read).
KEY_SCHEMA = "1"

T = TypeVar("T")


def normalize_query(text: str) -> str:
    """NFC, trimmed, internal whitespace collapsed. Case is preserved on purpose (see docs: embedding models and
    exact-match terms are case sensitive, so folding case could serve another query's vector)."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def digest(*parts: Any) -> str:
    """Stable sha256 of JSON-serialisable parts (field boundaries cannot be confused)."""
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def encode_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}d", *vec)


def decode_vector(raw: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(raw) // 8}d", raw))


@dataclass
class LayerCounters:
    hit: int = 0
    miss: int = 0
    bypass: int = 0
    coalesced: int = 0
    error: int = 0
    skipped_size: int = 0


class QueryCache:
    """One per effective cache configuration; holds the Redis clients, breaker and in-process counters."""

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.cache_enabled
        self.url = settings.cache_redis_url
        self.ttl = {
            "embedding": settings.cache_embedding_ttl_seconds,
            "retrieval": settings.cache_retrieval_ttl_seconds,
            "answer": settings.cache_answer_ttl_seconds,
        }
        self.max_bytes = settings.cache_max_value_bytes
        self.timeout = settings.cache_timeout_ms / 1000.0
        self.breaker_seconds = settings.cache_breaker_seconds
        self.fingerprint = digest(
            KEY_SCHEMA,
            settings.vector_store,
            settings.vector_metric,
            settings.vector_index,
            settings.hnsw_m,
            settings.hnsw_ef_construction,
            settings.hnsw_ef_search,
            settings.ivfflat_lists,
            settings.ivfflat_probes,
            settings.chroma_collection,
            settings.embedding_provider,
            settings.embedding_model,
            settings.embedding_dimension,
            settings.retrieval_fts_config,
            settings.chunk_size,
            settings.chunk_overlap,
            # deployment identity (a hash, never the URL): two deployments sharing a Redis db never share entries
            hashlib.sha256(settings.database_url.encode()).hexdigest(),
        )
        self.counters: dict[str, LayerCounters] = {name: LayerCounters() for name in LAYERS}
        self.started = time.time()
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis] = (
            weakref.WeakKeyDictionary()
        )
        self._sync: redis.Redis | None = None
        self._down_until = 0.0
        self._warned = False
        self._needs_purge = False
        self._inflight: dict[tuple[int, str], asyncio.Future[Any]] = {}
        self.last_error: str | None = None

    # ── availability ─────────────────────────────────────────────────────────────────────────
    def layer_on(self, layer: str) -> bool:
        return self.enabled and self.ttl[layer] > 0

    @property
    def breaker_open(self) -> bool:
        return time.monotonic() < self._down_until

    def _failed(self, exc: BaseException, op: str) -> None:
        self._down_until = time.monotonic() + self.breaker_seconds
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]
        if not self._warned:
            self._warned = True
            logger.warning(
                "query cache unavailable (%s: %s); serving uncached, retrying every %.0fs",
                op,
                exc,
                self.breaker_seconds,
            )

    def _recovered(self) -> None:
        if self._warned:
            self._warned = False
            logger.info("query cache available again")

    def _client(self) -> aioredis.Redis:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = aioredis.from_url(
                self.url,
                socket_timeout=self.timeout,
                socket_connect_timeout=self.timeout,
                decode_responses=False,
            )
            self._clients[loop] = client
        return client

    # ── raw operations (fail-open) ───────────────────────────────────────────────────────────
    async def _get(self, key: str) -> bytes | None:
        if self.breaker_open:
            return None
        try:
            raw = await asyncio.wait_for(self._client().get(key), self.timeout * 2)
            self._recovered()
            return raw if isinstance(raw, bytes) else None
        except Exception as exc:
            self._failed(exc, "get")
            return None

    async def _set(self, layer: str, key: str, value: bytes) -> bool:
        if len(value) > self.max_bytes:
            self.counters[layer].skipped_size += 1
            logger.debug(
                "cache value for %s skipped: %d bytes > %d", layer, len(value), self.max_bytes
            )
            return False
        if self.breaker_open:
            return False
        try:
            await asyncio.wait_for(
                self._client().set(key, value, ex=self.ttl[layer]), self.timeout * 2
            )
            return True
        except Exception as exc:
            self._failed(exc, "set")
            return False

    # ── index version ────────────────────────────────────────────────────────────────────────
    async def index_version(self) -> str | None:
        """The current index version token, or ``None`` when Redis is unavailable (callers then skip the cache)."""
        if self.breaker_open:
            return None
        if self._needs_purge and not await self.purge(("retrieval",)):
            return None
        try:
            client = self._client()
            raw = await asyncio.wait_for(client.get(VERSION_KEY), self.timeout * 2)
            if raw is None:
                # first use, or the key was evicted: a fresh random token can never match an old entry
                await asyncio.wait_for(
                    client.set(VERSION_KEY, uuid.uuid4().hex, nx=True), self.timeout * 2
                )
                raw = await asyncio.wait_for(client.get(VERSION_KEY), self.timeout * 2)
            self._recovered()
            return raw.decode() if isinstance(raw, bytes) else None
        except Exception as exc:
            self._failed(exc, "version")
            return None

    def _sync_client(self) -> redis.Redis:
        if self._sync is None:
            self._sync = redis.Redis.from_url(
                self.url,
                socket_timeout=max(self.timeout, 0.5),
                socket_connect_timeout=max(self.timeout, 0.5),
            )
        return self._sync

    def bump_sync(self, attempts: int = 3) -> bool:
        """Invalidate every retrieval entry (blocking; call via ``abump`` from async code). Never raises.

        A random token (not an incrementing counter) is stored, so an evicted/reset key can never make an old
        entry reachable again. If Redis cannot be reached this process remembers to purge the retrieval layer
        as soon as it is back (a *different* process that failed to bump is covered only by the TTL).
        """
        if not self.enabled:
            return True
        last: Exception | None = None
        for i in range(attempts):
            try:
                pipe = self._sync_client().pipeline()
                pipe.set(VERSION_KEY, uuid.uuid4().hex)
                pipe.incr(BUMPS_KEY)
                pipe.execute()
                self._needs_purge = False
                return True
            except Exception as exc:
                last = exc
                time.sleep(0.05 * (i + 1))
        self._needs_purge = True
        logger.error(
            "could not invalidate the query cache after an index change (%s); will purge on recovery",
            last,
        )
        return False

    async def abump(self) -> bool:
        return await asyncio.to_thread(self.bump_sync)

    # ── generic get-or-compute with single flight ────────────────────────────────────────────
    async def cached(
        self,
        layer: str,
        key: str,
        compute: Callable[[], Awaitable[T]],
        encode: Callable[[T], bytes | None],
        decode: Callable[[bytes], T],
        *,
        bypass: bool = False,
    ) -> tuple[T, str]:
        """Return ``(value, state)``; state is hit | miss | coalesced | bypass | off.

        ``compute`` runs at most once per key at a time in this process. Its exceptions propagate and nothing is
        stored; ``encode`` may return ``None`` to veto storing (e.g. an empty answer).
        """
        if not self.layer_on(layer):
            return await compute(), "off"
        counters = self.counters[layer]
        if bypass:
            counters.bypass += 1
            return await compute(), "bypass"
        raw = await self._get(key)
        if raw is not None:
            try:
                value = decode(raw)
            except Exception:
                logger.warning("undecodable %s cache entry dropped", layer)
                counters.error += 1
            else:
                counters.hit += 1
                return value, "hit"
        loop_key = (id(asyncio.get_running_loop()), key)
        while True:
            fut = self._inflight.get(loop_key)
            if fut is None:
                break
            try:
                value = await asyncio.shield(fut)
            except asyncio.CancelledError:
                if fut.cancelled() and not _self_cancelling():
                    continue  # the leader was cancelled, not us: compete to become the new leader
                raise
            counters.coalesced += 1
            return value, "coalesced"
        fut = asyncio.get_running_loop().create_future()
        fut.add_done_callback(lambda f: None if f.cancelled() else f.exception())  # mark retrieved
        self._inflight[loop_key] = fut
        try:
            counters.miss += 1
            value = await compute()
            payload = encode(value)
            if payload is not None:
                await self._set(layer, key, payload)
            fut.set_result(value)
            return value, "miss"
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                fut.cancel()
            else:
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(loop_key, None)

    # ── split get / put (streaming: the value only exists once the stream finished) ───────────
    async def lookup(
        self, layer: str, key: str, decode: Callable[[bytes], T], *, bypass: bool = False
    ) -> tuple[T | None, str]:
        """Read-only half of ``cached``: ``(value, "hit")`` or ``(None, miss | bypass | off)``. No single flight."""
        if not self.layer_on(layer):
            return None, "off"
        counters = self.counters[layer]
        if bypass:
            counters.bypass += 1
            return None, "bypass"
        raw = await self._get(key)
        if raw is not None:
            try:
                value = decode(raw)
            except Exception:
                logger.warning("undecodable %s cache entry dropped", layer)
                counters.error += 1
            else:
                counters.hit += 1
                return value, "hit"
        counters.miss += 1
        return None, "miss"

    async def put(self, layer: str, key: str, payload: bytes | None) -> bool:
        """Write-only half of ``cached``; call only after the computation finished cleanly."""
        if payload is None or not self.layer_on(layer):
            return False
        return await self._set(layer, key, payload)

    # ── admin ────────────────────────────────────────────────────────────────────────────────
    async def purge(self, layers: tuple[str, ...] = LAYERS) -> int | None:
        """Delete this cache's entries of ``layers`` (SCAN + DEL under the prefix only). ``None`` = Redis down."""
        try:
            client = self._client()
            removed = 0
            for layer in layers:
                batch: list[bytes] = []
                async for k in client.scan_iter(match=f"{PREFIX}{layer}:*", count=500):
                    batch.append(k)
                    if len(batch) >= 500:
                        removed += int(await client.delete(*batch))
                        batch = []
                if batch:
                    removed += int(await client.delete(*batch))
            if "retrieval" in layers:
                await client.set(VERSION_KEY, uuid.uuid4().hex)
                self._needs_purge = False
            self._recovered()
            return removed
        except Exception as exc:
            self._failed(exc, "purge")
            return None

    async def redis_stats(self) -> dict[str, Any]:
        """Live view for ``GET /cache/stats`` (keys per layer are counted with a bounded SCAN)."""
        info: dict[str, Any] = {
            "reachable": False,
            "keys": {},
            "index_version": None,
            "index_bumps": None,
        }
        try:
            client = self._client()
            await asyncio.wait_for(client.ping(), self.timeout * 4)
            info["reachable"] = True
            for layer in LAYERS:
                n = 0
                async for _ in client.scan_iter(match=f"{PREFIX}{layer}:*", count=1000):
                    n += 1
                    if n >= 100_000:
                        break
                info["keys"][layer] = n
            ver = await client.get(VERSION_KEY)
            info["index_version"] = ver.decode()[:8] if isinstance(ver, bytes) else None
            bumps = await client.get(BUMPS_KEY)
            info["index_bumps"] = int(bumps) if bumps else 0
            mem = await client.info("memory")
            info["used_memory_bytes"] = int(mem.get("used_memory", 0))
            info["maxmemory_policy"] = (await client.config_get("maxmemory-policy")).get(
                "maxmemory-policy"
            )
        except Exception as exc:
            self._failed(exc, "stats")
            info["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return info

    def local_stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "breaker_open": self.breaker_open,
            "last_error": self.last_error,
            "since": self.started,
            "layers": {
                name: {
                    **vars(self.counters[name]),
                    "ttl_seconds": self.ttl[name],
                    "on": self.layer_on(name),
                }
                for name in LAYERS
            },
        }

    def describe(self) -> dict[str, Any]:
        host = self.url.split("@")[-1]  # never log credentials embedded in the URL
        return {
            "enabled": self.enabled,
            "redis": host,
            "ttl_seconds": self.ttl,
            "max_value_bytes": self.max_bytes,
            "timeout_ms": int(self.timeout * 1000),
            "breaker_seconds": self.breaker_seconds,
        }


def _self_cancelling() -> bool:
    task = asyncio.current_task()
    return bool(task is not None and task.cancelling())


# ── per-request view ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CacheCall:
    """What one request did with the cache: fed to the response headers and the trace."""

    cache: QueryCache
    bypass: bool = False
    layers: dict[str, str] = field(default_factory=dict)
    #: LLM completion avoided by an answer hit, and the money it would have cost (None = cost unknown).
    llm_calls_avoided: int = 0
    saved_usd: float | None = None
    #: Embedding-provider calls avoided relative to running with the cache off.
    embeddings_avoided: int = 0

    @property
    def active(self) -> bool:
        return self.cache.enabled

    def result(self, final_layer: str) -> str:
        """The request-level verdict: hit when the terminal layer served from cache."""
        if not self.cache.enabled:
            return "off"
        if self.bypass:
            return "bypass"
        state = self.layers.get(final_layer) or self.layers.get(
            "retrieval"
        )  # chat without context skips L3
        return "hit" if state == "hit" else "miss"

    def header(self, final_layer: str) -> dict[str, str]:
        detail = ",".join(f"{k}={v}" for k, v in self.layers.items())
        out = {"X-Cache": self.result(final_layer)}
        if detail:
            out["X-Cache-Layers"] = detail
        return out

    def trace_meta(self, final_layer: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"result": self.result(final_layer), **self.layers}
        if self.embeddings_avoided:
            meta["embeddings_avoided"] = self.embeddings_avoided
        if self.llm_calls_avoided:
            meta["llm_calls_avoided"] = self.llm_calls_avoided
            meta["saved_usd"] = self.saved_usd
        return meta


def wants_bypass(body_cache: bool | None, cache_control: str | None) -> bool:
    """``cache: false`` in the body or ``Cache-Control: no-cache`` / ``no-store`` on the request."""
    if body_cache is False:
        return True
    directives = {d.strip().lower().split("=")[0] for d in (cache_control or "").split(",")}
    return bool(directives & {"no-cache", "no-store"})


_instances: dict[str, QueryCache] = {}


def get_cache(settings: Settings) -> QueryCache:
    """The shared ``QueryCache`` of this process for ``settings`` (one per distinct cache configuration)."""
    key = digest(
        settings.cache_enabled,
        settings.cache_redis_url,
        settings.cache_embedding_ttl_seconds,
        settings.cache_retrieval_ttl_seconds,
        settings.cache_answer_ttl_seconds,
        settings.cache_max_value_bytes,
        settings.cache_timeout_ms,
        settings.cache_breaker_seconds,
        settings.database_url,
        settings.vector_store,
        settings.vector_metric,
        settings.vector_index,
        settings.hnsw_m,
        settings.hnsw_ef_construction,
        settings.hnsw_ef_search,
        settings.ivfflat_lists,
        settings.ivfflat_probes,
        settings.chroma_collection,
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_dimension,
        settings.retrieval_fts_config,
        settings.chunk_size,
        settings.chunk_overlap,
    )
    found = _instances.get(key)
    if found is None:
        found = _instances[key] = QueryCache(settings)
    return found


_process_settings: Settings | None = None


def bump_index_version() -> bool:
    """Invalidate cached retrieval results after any change to the searchable index. Blocking, never raises.

    Called from every mutation path (ingest, delete, replace, reindex, rebuild-fts, rebuild-index).
    """
    global _process_settings
    try:
        if _process_settings is None:
            _process_settings = Settings()
        return get_cache(_process_settings).bump_sync()
    except Exception:
        logger.exception("cache invalidation failed")
        return False


async def abump_index_version() -> bool:
    return await asyncio.to_thread(bump_index_version)


def log_effective_settings(settings: Settings) -> None:
    logger.info("query cache: %s", get_cache(settings).describe())
