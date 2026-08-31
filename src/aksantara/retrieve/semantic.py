"""Semantic retrieval for Aksantara.

Third in the ``exact → prefix → semantic`` cascade. Embeds the query with
``RETRIEVAL_QUERY`` (768-d), runs Firestore KNN with DOT_PRODUCT and a
fail-closed ``distance_threshold``, then resolves Firestore hits back to
canonical ``KBBIEntry`` records. Unknown or weak queries return an empty
list — never a low-confidence authoritative claim.

This module depends only on port protocols (EmbeddingStore / Firestore-like
collection) so unit tests can inject in-memory fakes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aksantara.domain.models import KBBIEntry
from aksantara.embeddings.document import build_embedding_document

logger = logging.getLogger(__name__)

DEFAULT_DISTANCE_THRESHOLD: float = 0.70
DEFAULT_DISTANCE_RESULT_FIELD: str = "vector_distance"
DEFAULT_SEMANTIC_LIMIT: int = 10
DEFAULT_DIMS: int = 768

__all__ = [
    "SemanticHit",
    "SemanticRetriever",
    "retrieve_semantic",
    "retrieve_with_fallback",
]


def retrieve_semantic(
    query: str,
    entry_store: object,
    vector_store: object,
    embed_client: object,
    *,
    limit: int = 5,
    distance_threshold: float = 0.6,
    filter_source_kind: str | None = "official-live",
) -> list[dict[str, object]]:
    """Back-compat functional wrapper for simple tests.

    Uses exact→prefix short-circuit then KNN via vector_store.find_nearest.
    """
    from aksantara.retrieve.exact import InMemoryEntryStore

    q = query.strip()
    if not q:
        return []
    # short-circuit exact/prefix if entry_store is InMemoryEntryStore
    if isinstance(entry_store, InMemoryEntryStore):
        from aksantara.retrieve.exact import retrieve_exact
        from aksantara.retrieve.prefix import retrieve_prefix

        exact = retrieve_exact(q, entry_store)  # type: ignore[arg-type]
        if exact is not None:
            return [{"entry": exact, "mode": "exact", "distance": 0.0}]
        prefix_hits = retrieve_prefix(q, entry_store, limit=limit)  # type: ignore[arg-type]
        if prefix_hits and len(q) >= 3:
            return [
                {"entry": e, "mode": "prefix", "distance": 0.0} for e in prefix_hits
            ]
    # semantic
    try:
        q_vec = embed_client.embed(q, task_type="RETRIEVAL_QUERY")  # type: ignore[attr-defined, call-arg]
    except Exception:
        try:
            q_vec = embed_client.embed(q)  # type: ignore[attr-defined]
        except Exception:
            return []
    try:
        candidates = vector_store.find_nearest(  # type: ignore[attr-defined]
            q_vec,
            limit=limit,
            distance_threshold=distance_threshold,
            filter_source_kind=filter_source_kind,
        )
    except Exception:
        return []
    if not candidates:
        return []
    results: list[dict[str, object]] = []
    for rec, dist in candidates:  # type: ignore[misc]
        # rec may be VectorRecord or dict
        entry_id = getattr(rec, "entry_id", None) or getattr(rec, "id", None) or ""
        lema = getattr(rec, "lema", None) or ""
        entry = None
        if isinstance(entry_store, InMemoryEntryStore):
            entry = entry_store.get_by_id(entry_id) or entry_store.get_by_lema(lema)  # type: ignore[arg-type]
        if entry is None:
            continue
        if entry.status != "active" or entry.source.source_kind not in {
            "official-live",
            "official-snapshot",
        }:
            continue
        results.append(
            {
                "entry": entry,
                "mode": "semantic",
                "distance": dist,
                "vector_id": entry_id,
            }
        )
    return results


def retrieve_with_fallback(*args: object, **kwargs: object) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    return retrieve_semantic(*args, **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SemanticHit:
    """One semantic result with citation-grade metadata."""

    entry: KBBIEntry
    distance: float
    distance_result_field: str = DEFAULT_DISTANCE_RESULT_FIELD
    threshold: float = DEFAULT_DISTANCE_THRESHOLD


class SemanticRetriever:
    """Fail-closed semantic search adapter.

    Args:
        embedding_client: object with ``embed_query(text) -> list[float]``.
        vector_store: object with ``find_nearest(query_vector, limit,
            distance_threshold, ...) -> list[NearestResult]`` — typically
            ``FirestoreVectorStore`` or an in-memory fake implementing the
            same method.
        canonical_index: optional lookup from ``lema`` or ``id`` to
            ``KBBIEntry`` used to hydrate semantic hits. When not supplied,
            a Firestore ``entries`` collection (if a client was provided via
            the vector store) is probed; otherwise hits without a hydrateable
            canonical entry are dropped.
        firestore_client: optional Firestore client for hydration fallback
            when canonical_index misses.
        distance_threshold: inclusive DOT_PRODUCT floor.
        distance_result_field: Firestore distance field name.
        default_limit: default max hits.
    """

    def __init__(
        self,
        *,
        embedding_client: Any,
        vector_store: Any,
        canonical_index: Any | None = None,
        firestore_client: Any | None = None,
        distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
        distance_result_field: str = DEFAULT_DISTANCE_RESULT_FIELD,
        default_limit: int = DEFAULT_SEMANTIC_LIMIT,
    ) -> None:
        self._embedder = embedding_client
        self._store = vector_store
        self._index = canonical_index
        self._client = firestore_client
        self._threshold = distance_threshold
        self._distance_field = distance_result_field
        self._default_limit = default_limit

    def _hydrate(self, doc_id: str, raw: dict[str, Any] | None) -> KBBIEntry | None:
        # Try in-memory index first.
        identifier = doc_id.split("_")[0] if "_" in doc_id else doc_id
        candidate_lema = ""
        if raw is not None:
            candidate_lema = str(raw.get("lema", "")).strip().lower()

        # Index may expose get_by_lema / get_by_id / __getitem__.
        for lookup_key in (candidate_lema, identifier, doc_id):
            if not lookup_key:
                continue
            if self._index is not None:
                if hasattr(self._index, "get_by_lema"):
                    try:
                        e = self._index.get_by_lema(lookup_key)  # type: ignore[attr-defined]
                        if e is not None:
                            return e
                    except Exception:
                        pass
                if hasattr(self._index, "get_by_id"):
                    try:
                        e = self._index.get_by_id(lookup_key)  # type: ignore[attr-defined]
                        if e is not None:
                            return e
                    except Exception:
                        pass
                if isinstance(self._index, dict):
                    e = self._index.get(lookup_key)
                    if isinstance(e, KBBIEntry):
                        return e
                    # Case-insensitive fallback for dict keyed by lowercase lema.
                    e2 = self._index.get(lookup_key.lower())
                    if isinstance(e2, KBBIEntry):
                        return e2

        # Firestore hydration fallback when an index miss occurs and a client
        # was supplied.
        if self._client is not None:
            for key in (identifier, candidate_lema, doc_id):
                if not key:
                    continue
                try:
                    doc = self._client.collection("entries").document(key).get()
                    exists = getattr(doc, "exists", True)
                    if callable(exists):
                        has = doc.exists  # type: ignore[attr-defined]
                    else:
                        has = bool(exists)
                    if not has:
                        continue
                    data = doc.to_dict() if hasattr(doc, "to_dict") else {}
                    if isinstance(data, dict) and data:
                        return KBBIEntry.model_validate(data)
                except Exception:
                    continue

        # As a last resort, if raw already contains a serializable canonical
        # entry shape, attempt to validate it; some fakes store the full entry
        # in the vector record's raw dict.
        if raw is not None and "makna" in raw and "source" in raw:
            try:
                return KBBIEntry.model_validate(raw)
            except Exception:
                pass

        return None

    def search(self, query: str, *, limit: int | None = None) -> list[SemanticHit]:
        """Execute semantic search, fail-closed.

        Args:
            query: user query text. Blank queries yield an empty result.
            limit: override hits cap.

        Returns:
            Up to ``limit`` hits, each with ``distance >= threshold``, sorted
            by descending distance. The list is empty when the query is blank,
            embedding fails, or no hit clears the threshold.
        """
        cleaned = query.strip()
        if not cleaned:
            return []
        cap = min(self._default_limit if limit is None else limit, 50)
        if cap <= 0:
            return []

        # Embed query with RETRIEVAL_QUERY.
        try:
            qvec = self._embedder.embed_query(cleaned)
        except Exception as exc:
            logger.warning("semantic embed_query failed for %r: %s", cleaned, exc)
            return []

        if not isinstance(qvec, (list, tuple)) or len(qvec) != DEFAULT_DIMS:
            logger.warning(
                "semantic query vector invalid length %s, expected %d",
                len(qvec) if isinstance(qvec, (list, tuple)) else type(qvec).__name__,
                DEFAULT_DIMS,
            )
            return []

        # KNN with threshold.
        try:
            nearest = self._store.find_nearest(
                list(qvec),
                limit=cap,
                distance_threshold=self._threshold,
            )
        except Exception as exc:
            logger.warning("semantic find_nearest failed: %s", exc)
            return []
        if not nearest:
            return []

        hits: list[SemanticHit] = []
        for nr in nearest:
            # Unwrap the NearestResult-like shape — supports both the
            # firestore.NearestResult dataclass, dict-based fakes, and
            # tuple-pair (VectorRecord, distance) from InMemoryVectorStore.
            distance: float | None = None
            raw: dict[str, Any] | None = None
            doc_id: str = ""
            tuple_record: Any | None = None
            tuple_distance: float | None = None
            # Tuple pair from InMemoryVectorStore: (VectorRecord, 1-dot)
            if (
                isinstance(nr, (tuple, list))
                and len(nr) == 2
                and not isinstance(nr, dict)
            ):
                rec_t, dist_t = nr  # type: ignore[misc]
                tuple_record = rec_t
                try:
                    tuple_distance = float(dist_t)
                except Exception:
                    continue
                # Convert InMemory DOT_PRODUCT distance (1-dot) back to dot for threshold check
                # Heuristic: InMemory distance is 1-dot; spec threshold is dot 0.70
                # If raw distance looks like small (0.0-0.3) for near match, convert.
                # Legacy store uses DOT_PRODUCT with distance = 1-dot.
                if tuple_distance is not None:
                    # If the store's distance_measure is DOT_PRODUCT, this is 1-dot
                    distance = 1.0 - tuple_distance
                else:
                    distance = None
                raw = (
                    getattr(rec_t, "metadata", None)
                    if hasattr(rec_t, "metadata")
                    else None
                )
                if raw is None and hasattr(rec_t, "to_firestore_dict"):
                    try:
                        raw = rec_t.to_firestore_dict()  # type: ignore[attr-defined]
                    except Exception:
                        raw = None
                doc_id = (
                    getattr(rec_t, "doc_id", None)
                    or getattr(rec_t, "entry_id", None)
                    or getattr(rec_t, "id", "")
                )  # type: ignore[attr-defined]
                if not isinstance(doc_id, str):
                    doc_id = str(doc_id)
                # Store tuple_record for hydrate path below
                nr = {
                    "_tuple_record": rec_t,
                    "_tuple_distance": distance,
                    "raw": raw,
                    "distance": distance,
                    "doc_id": doc_id,
                }  # type: ignore[assignment]
                raw = raw if isinstance(raw, dict) else None
                # is_match handled after conversion
                if (
                    tuple_distance is not None
                    and distance is not None
                    and distance < self._threshold
                ):
                    continue
            elif isinstance(nr, dict):
                raw = nr.get("raw") or nr
                distance = nr.get("distance") or (
                    raw.get(self._distance_field) if isinstance(raw, dict) else None
                )
                doc_id = str(
                    nr.get("doc_id")
                    or (raw.get("id", "") if isinstance(raw, dict) else "")
                    or nr.get("id", "")
                )
                if nr.get("is_match") is False:
                    continue
                tuple_record = nr.get("_tuple_record")
            else:
                raw = getattr(nr, "raw", None)
                distance = getattr(nr, "distance", None)
                rec = getattr(nr, "record", None)
                if rec is not None and hasattr(rec, "doc_id"):
                    doc_id = rec.doc_id  # type: ignore[attr-defined]
                elif raw is not None and isinstance(raw, dict):
                    doc_id = str(raw.get("id", ""))
                is_match = getattr(nr, "is_match", True)
                if is_match is False:
                    continue
                tuple_record = getattr(nr, "record", None)

            if distance is None and isinstance(raw, dict):
                distance = raw.get(self._distance_field)
            if distance is None:
                continue
            try:
                score = float(distance)
            except Exception:
                continue
            if score < self._threshold:
                continue

            # Hydrate canonical entry.
            entry: KBBIEntry | None = None
            # Tuple path from InMemoryVectorStore — hydrate via stored record
            if tuple_record is not None:
                # Try hydrate via tuple_record's id/lema through canonical index
                hydrated = self._hydrate(
                    doc_id
                    or getattr(tuple_record, "id", "")
                    or getattr(tuple_record, "entry_id", ""),
                    raw,
                )
                if hydrated is not None:
                    entry = hydrated
                else:
                    # Fallback: if tuple_record lema matches index, use that
                    lema_fallback = getattr(tuple_record, "lema", "")
                    if lema_fallback:
                        entry = self._hydrate(lema_fallback, raw)
                if entry is None:
                    continue
            else:
                rec2 = getattr(nr, "record", None) if not isinstance(nr, dict) else None
                if rec2 is not None and isinstance(rec2, KBBIEntry):
                    entry = rec2
                elif rec2 is not None and hasattr(rec2, "lema"):
                    hydrated = self._hydrate(doc_id or getattr(rec2, "id", ""), raw)
                    entry = hydrated if hydrated is not None else None
                    if entry is None:
                        continue
                else:
                    entry = self._hydrate(doc_id, raw)
                if entry is None:
                    continue

            if entry.status != "active" or entry.source.source_kind not in {
                "official-live",
                "official-snapshot",
            }:
                continue
            hits.append(
                SemanticHit(
                    entry=entry,
                    distance=score,
                    distance_result_field=self._distance_field,
                    threshold=self._threshold,
                )
            )

        hits.sort(key=lambda h: h.distance, reverse=True)
        return hits[:cap]

    def __call__(self, query: str, *, limit: int | None = None) -> list[SemanticHit]:
        return self.search(query, limit=limit)

    # Convenience for callers that already have KBBIEntry and want the
    # embedding document without importing document.py.
    @staticmethod
    def embedding_document(entry: KBBIEntry) -> str:
        return build_embedding_document(entry)
