"""Vector-store seam: one protocol, two backends (Chroma default, pgvector optional).

Contract shared by every backend (and enforced by ``tests/integration/test_vector_store_contract.py``):

* **Ids.** A chunk may carry an explicit ``id`` (content-addressed, ``services.incremental``);
  otherwise it is ``{doc_id}_{position}``. ``add_chunks`` is an *upsert*; duplicate ids inside one
  call are an error.
* **Metadata.** Every vector carries ``{"doc_id", "index", ...primitive extras}``. ``update_metadata``
  *merges* into the existing metadata and never touches the embedding or the text.
* **Filters.** ``{"doc_id": "x"}`` (equality), ``{"doc_id": {"$in": [...]}}``; several keys are ANDed.
* **Scores are distances: lower = closer, in every metric, with the same numbers on both stores.**
  ``cosine`` = ``1 - cos`` (0..2), ``l2`` = *squared* euclidean distance, ``ip`` = ``1 - dot``
  (can be negative). pgvector's native operators differ (``<=>`` cosine distance, ``<->`` plain
  euclidean, ``<#>`` *negative* dot product), so ``PgVectorStore`` converts them to this convention.
  Results come back ordered ascending by score.
* **Settings drift.** ``verify()`` compares what is actually stored (read back from Chroma's collection
  configuration / Postgres catalogs) with the configured settings and raises
  ``VectorStoreMismatchError`` naming the reindex path instead of silently returning bad results.
  ``reset()`` drops and recreates the store with the *configured* settings; it is the reindex hook.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast, runtime_checkable

from app.config import VECTOR_STORES, Settings

logger = logging.getLogger(__name__)


_VALID_VECTOR_STORES = VECTOR_STORES

REINDEX_HINT = (
    "Rebuild the vector index from the stored chunk text with `python -m app.manage reindex` "
    "(or the Celery task app.tasks.reindex.reindex_vectors), or revert the VECTOR_* / EMBEDDING_* "
    "settings to the stored ones. See docs/configuration.md#changing-the-vector-store."
)


class VectorStoreMismatchError(RuntimeError):
    """The stored index does not match the configured settings (store, metric, index, dimension)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Vector store does not match the configuration: " + "; ".join(problems) + ". " + REINDEX_HINT)


@dataclass(frozen=True)
class VectorSettings:
    """The *effective* vector settings after applying per-store defaults."""

    store: str
    metric: str
    index: str
    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
    hnsw_ef_search: int | None = None
    ivfflat_lists: int | None = None
    ivfflat_probes: int | None = None
    dimension: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_vector_settings(settings: Settings, store: str | None = None) -> VectorSettings:
    """Apply store defaults to the optional settings.

    Chroma: metric ``l2`` (what an existing collection already uses), HNSW params left to Chroma
    unless set. pgvector: cosine + hnsw 16/64/40, ivfflat 100 lists / 10 probes.
    """
    from app.services.embeddings import configured_dimension

    store = store or settings.vector_store
    dimension = configured_dimension(settings)
    if store == "chroma":
        return VectorSettings(
            store="chroma",
            metric=settings.vector_metric or "l2",
            index="hnsw",
            hnsw_m=settings.hnsw_m,
            hnsw_ef_construction=settings.hnsw_ef_construction,
            hnsw_ef_search=settings.hnsw_ef_search,
            dimension=dimension,
        )
    return VectorSettings(
        store=store,
        metric=settings.vector_metric or "cosine",
        index=settings.vector_index or "hnsw",
        hnsw_m=settings.hnsw_m or 16,
        hnsw_ef_construction=settings.hnsw_ef_construction or 64,
        hnsw_ef_search=settings.hnsw_ef_search or 40,
        ivfflat_lists=settings.ivfflat_lists or 100,
        ivfflat_probes=settings.ivfflat_probes or 10,
        dimension=dimension,
    )


def compare_settings(configured: VectorSettings, stored: dict[str, Any]) -> list[str]:
    """Differences between configured settings and what a store reports (``[]`` = compatible).

    ``stored`` keys the store cannot know are ``None`` and skipped (e.g. the metric of an
    un-indexed pgvector table is a pure query-time choice; a Chroma collection with no vectors
    has no dimension yet). Only parameters relevant to the index type are compared.
    """
    problems: list[str] = []

    def diff(label: str, want: Any, have: Any) -> None:
        if want is not None and have is not None and want != have:
            problems.append(f"{label}: stored {have}, configured {want}")

    diff("VECTOR_METRIC", configured.metric, stored.get("metric"))
    diff("VECTOR_INDEX", configured.index, stored.get("index"))
    diff("embedding dimension", configured.dimension, stored.get("dimension"))
    if configured.index == "hnsw" and stored.get("index") in (None, "hnsw"):
        diff("HNSW_M", configured.hnsw_m, stored.get("hnsw_m"))
        diff("HNSW_EF_CONSTRUCTION", configured.hnsw_ef_construction, stored.get("hnsw_ef_construction"))
    if configured.index == "ivfflat" and stored.get("index") == "ivfflat":
        diff("IVFFLAT_LISTS", configured.ivfflat_lists, stored.get("ivfflat_lists"))
    return problems


@runtime_checkable
class VectorStore(Protocol):
    """Interface for vector-store backends (see the module docstring for the shared contract)."""

    def add_chunks(
        self,
        doc_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        """Persist chunks and their pre-computed embeddings."""
        ...

    def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the *top_k* nearest neighbours with scores (distances, lower = closer)."""
        ...

    def delete_doc(self, doc_id: str) -> None:
        """Remove all chunks belonging to *doc_id*."""
        ...

    def delete_ids(self, ids: list[str]) -> None:
        """Remove specific vectors by id (no-op for ids that do not exist)."""
        ...

    def update_metadata(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        """Merge new metadata into existing vectors without touching their embeddings."""
        ...

    def reset(self) -> None:
        """Drop and recreate the store with the *configured* settings (reindex hook, dev/test)."""
        ...

    def verify(self) -> None:
        """Create what is missing (pgvector schema) and raise ``VectorStoreMismatchError`` if the
        stored index disagrees with the configured settings."""
        ...

    def describe(self) -> dict[str, Any]:
        """``{"store", "configured", "stored", "count", "problems"}`` for logs, /health, the CLI."""
        ...


def _prepare_rows(
    doc_id: str, chunks: list[dict[str, Any]], embeddings: list[list[float]]
) -> list[tuple[str, str, dict[str, Any]]]:
    """Shared id/metadata rules: ``(id, text, metadata)`` per chunk (explicit id wins over position)."""
    if len(chunks) != len(embeddings):
        raise ValueError(f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) must have equal length.")
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for idx, chunk in enumerate(chunks):
        chunk_id = str(chunk.get("id") or f"{doc_id}_{idx}")
        metadata = {
            "doc_id": doc_id,
            "index": idx,
            **{k: v for k, v in chunk.get("metadata", {}).items() if isinstance(v, (str, int, float, bool))},
        }
        rows.append((chunk_id, chunk["text"], metadata))
    return rows


def _filter_clauses(filters: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    """Normalise a filter dict to ``[(key, [allowed values])]``; only equality and ``$in``."""
    clauses: list[tuple[str, list[Any]]] = []
    for key, value in filters.items():
        if isinstance(value, dict):
            if set(value) != {"$in"} or not isinstance(value["$in"], (list, tuple)):
                raise ValueError(f"Unsupported filter for {key!r}: only equality and {{'$in': [...]}} are supported.")
            clauses.append((key, list(value["$in"])))
        else:
            clauses.append((key, [value]))
    return clauses


_chroma_clients: dict[tuple[str, int], Any] = {}
_chroma_clients_lock = threading.Lock()


def _chroma_client(host: str, port: int) -> Any:
    """One ``chromadb.HttpClient`` per server for the process. A client per ``ChromaStore`` (one per request)
    leaked ~1.5 MB each: found by the ticket 37 benchmark, where the API container was OOM-killed. The collection
    handle is still fetched per store, so a collection recreated by another process (reindex) is picked up."""
    import chromadb

    with _chroma_clients_lock:
        client = _chroma_clients.get((host, port))
        if client is None:
            client = _chroma_clients[(host, port)] = chromadb.HttpClient(host=host, port=port)
        return client


class ChromaStore:
    """ChromaDB vector-store adapter (server mode via ``chromadb.HttpClient``).

    Index options go through the collection ``configuration`` (verified on Chroma server 1.4.x with
    chromadb-client 1.5.x): ``space`` (cosine/l2/ip), ``max_neighbors`` (HNSW M) and
    ``ef_construction`` are fixed at creation; ``ef_search`` can be changed later and is re-applied
    on ``verify()``. Chroma has no IVFFlat/``none`` option. A collection that already exists keeps
    its original configuration (Chroma ignores the one passed on re-open), hence ``verify()``.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        collection_name: str | None = None,
        vector_settings: VectorSettings | None = None,
    ) -> None:
        cfg0 = Settings()
        self._host = host or cfg0.chroma_host
        self._port = port or cfg0.chroma_port
        self.settings = vector_settings or resolve_vector_settings(cfg0, "chroma")
        self._client = _chroma_client(self._host, self._port)
        self._collection = self._client.get_or_create_collection(
            name=collection_name or cfg0.chroma_collection, configuration=cast(Any, self._configuration())
        )

    def _configuration(self) -> dict[str, Any]:
        s = self.settings
        hnsw: dict[str, Any] = {"space": s.metric}
        if s.hnsw_m is not None:
            hnsw["max_neighbors"] = s.hnsw_m
        if s.hnsw_ef_construction is not None:
            hnsw["ef_construction"] = s.hnsw_ef_construction
        if s.hnsw_ef_search is not None:
            hnsw["ef_search"] = s.hnsw_ef_search
        return {"hnsw": hnsw}

    def add_chunks(
        self,
        doc_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        rows = _prepare_rows(doc_id, chunks, embeddings)
        if not rows:
            return
        try:
            self._collection.upsert(
                ids=[r[0] for r in rows],
                documents=[r[1] for r in rows],
                # chromadb's stubs expect numpy arrays / Sequence[float]; list[list[float]] is
                # accepted at runtime and converted internally.
                embeddings=cast(Any, embeddings),
                metadatas=cast(Any, [r[2] for r in rows]),
            )
        except Exception as exc:
            if "dimension" in str(exc).lower():
                raise VectorStoreMismatchError([f"embedding dimension differs from the stored vectors ({exc})"]) from exc
            raise

    def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] | None = None
        if filters:
            # Chroma where filter expects a flat dict for simple equality.
            if len(filters) == 1:
                key, value = next(iter(filters.items()))
                where = {key: value}
            else:
                where = {"$and": [{k: v} for k, v in filters.items()]}

        try:
            results = self._collection.query(
                query_embeddings=cast(Any, [query_embedding]),
                n_results=top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            if "dimension" in str(exc).lower():
                raise VectorStoreMismatchError([f"query embedding dimension differs from the stored vectors ({exc})"]) from exc
            raise

        output: list[dict[str, Any]] = []
        ids = results["ids"]
        if not ids or not ids[0]:
            return output
        distances = results["distances"] or []
        documents = results["documents"] or []
        metadatas = results["metadatas"] or []

        for i, chunk_id in enumerate(ids[0]):
            output.append(
                {
                    "id": chunk_id,
                    "score": distances[0][i],
                    "text": documents[0][i] if documents else "",
                    "metadata": metadatas[0][i] if metadatas else {},
                }
            )
        return output

    def delete_doc(self, doc_id: str) -> None:
        self._collection.delete(where={"doc_id": doc_id})

    def delete_ids(self, ids: list[str]) -> None:
        if ids:
            self._collection.delete(ids=ids)

    def update_metadata(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        if len(ids) != len(metadatas):
            raise ValueError("ids and metadatas must have equal length.")
        if ids:
            self._collection.update(ids=ids, metadatas=cast(Any, metadatas))

    #: ``reindex_all`` may rewrite this store in place (upsert + delete stale ids) while ``verify()`` is clean.
    #: Chroma keeps the HNSW index of a deleted collection in server memory (~17 MiB per 2768 x 1024 reindex).
    supports_in_place_reindex = True

    def all_ids(self) -> list[str]:
        """Every vector id in the collection (ids only: no documents or embeddings are fetched)."""
        ids: list[str] = []
        while True:
            page = self._collection.get(limit=5000, offset=len(ids), include=[])["ids"]
            ids.extend(page)
            if len(page) < 5000:
                return ids

    def reset(self) -> None:
        name = self._collection.name
        self._client.delete_collection(name=name)
        self._collection = self._client.get_or_create_collection(
            name=name, configuration=cast(Any, self._configuration())
        )

    # ── settings drift ──────────────────────────────────────────────────────────
    def stored_settings(self) -> dict[str, Any]:
        """What the collection really has, read back from the server."""
        col = self._client.get_collection(name=self._collection.name)
        cfg = col.configuration
        hnsw = (cfg.get("hnsw") if hasattr(cfg, "get") else None) or {}
        stored: dict[str, Any] = {
            "metric": hnsw.get("space"),
            "index": "hnsw" if hnsw else "spann",
            "hnsw_m": hnsw.get("max_neighbors"),
            "hnsw_ef_construction": hnsw.get("ef_construction"),
            "hnsw_ef_search": hnsw.get("ef_search"),
            "dimension": None,
        }
        if col.count() > 0:
            emb = col.get(limit=1, include=["embeddings"])["embeddings"]
            if emb is not None and len(emb) > 0:
                stored["dimension"] = len(emb[0])
        return stored

    def verify(self) -> None:
        stored = self.stored_settings()
        problems = compare_settings(self.settings, stored)
        if problems:
            raise VectorStoreMismatchError(problems)
        want = self.settings.hnsw_ef_search
        if want is not None and stored.get("hnsw_ef_search") != want:
            # ef_search is a query-time knob: Chroma lets us change it on an existing collection.
            self._collection.modify(configuration=cast(Any, {"hnsw": {"ef_search": want}}))

    def describe(self) -> dict[str, Any]:
        stored = self.stored_settings()
        return {
            "store": "chroma",
            "collection": self._collection.name,
            "configured": self.settings.as_dict(),
            "stored": stored,
            "count": self._collection.count(),
            "problems": compare_settings(self.settings, stored),
        }


# ── factory ──────────────────────────────────────────────────────────────────────

#: Verification is cheap but not free; a verified store is trusted for this long per process.
_VERIFY_TTL_SECONDS = 60.0
_verified: dict[tuple[Any, ...], float] = {}
_verify_lock = threading.Lock()


def get_vector_store(
    vector_store: str | None = None, *, settings: Settings | None = None, verify: bool = True
) -> VectorStore:
    """Factory that returns the configured vector-store backend.

    With ``verify=True`` (default) the store is verified at most once per minute per process:
    pgvector creates its schema/index idempotently, and both stores raise
    ``VectorStoreMismatchError`` when the stored index disagrees with the configuration.
    """
    cfg = settings or Settings()
    vector_store = vector_store or cfg.vector_store

    if vector_store not in _VALID_VECTOR_STORES:
        raise ValueError(
            f"Unknown vector store '{vector_store}'. "
            f"Supported: {', '.join(_VALID_VECTOR_STORES)}."
        )

    store: VectorStore
    if vector_store == "chroma":
        store = ChromaStore(collection_name=cfg.chroma_collection, vector_settings=resolve_vector_settings(cfg, "chroma"))
    elif vector_store == "pgvector":
        from app.services.pgvector_store import PgVectorStore

        store = PgVectorStore.from_settings(cfg)
    else:  # pragma: no cover - guarded above
        raise ValueError(f"Unhandled vector store '{vector_store}'.")

    if verify:
        key = (cfg.database_url, cfg.chroma_host, cfg.chroma_port, cfg.chroma_collection, repr(resolve_vector_settings(cfg, vector_store)))
        now = time.monotonic()
        with _verify_lock:
            fresh = now - _verified.get(key, -1e9) < _VERIFY_TTL_SECONDS
        if not fresh:
            store.verify()
            with _verify_lock:
                _verified[key] = now
    return store


def log_effective_settings(store: VectorStore | None = None, *, settings: Settings | None = None) -> None:
    """Startup log line with the effective vector settings; logs an error on a mismatch, never raises."""
    cfg = settings or Settings()
    effective = resolve_vector_settings(cfg)
    logger.info("vector store: %s", effective.as_dict())
    try:
        (store or get_vector_store(settings=cfg, verify=False)).verify()
    except VectorStoreMismatchError as exc:
        logger.error("%s", exc)
    except Exception as exc:
        logger.warning("vector store not verified at startup: %s", exc)
