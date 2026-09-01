"""In-memory vector store for offline tests."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from aksantara.embeddings.firestore_types import (
    DEFAULT_DIMS,
    DEFAULT_DISTANCE_MEASURE,
    DEFAULT_DISTANCE_RESULT_FIELD,
    DEFAULT_DISTANCE_THRESHOLD,
    NearestResult,
    VectorRecord,
)
from aksantara.embeddings.firestore_utils import (
    _deduplicate_records,
    _record_doc_id,
    _record_fingerprint,
)

__all__ = ["EmbeddingStore", "InMemoryVectorStore"]


class EmbeddingStore(ABC):
    @abstractmethod
    def put(self, record: VectorRecord) -> None:  # pragma: no cover
        raise NotImplementedError

    @abstractmethod
    def put_many(self, records: Iterable[VectorRecord]) -> None:  # pragma: no cover
        raise NotImplementedError

    @abstractmethod
    def find_nearest(
        self,
        query_vector: list[float],
        *,
        limit: int = 5,
        distance_threshold: float | None = None,
        filter_source_kind: str | None = None,
        **kwargs: Any,
    ) -> Any:  # pragma: no cover
        raise NotImplementedError


class InMemoryVectorStore(EmbeddingStore):
    """Brute-force KNN for offline tests — supports both distance semantics."""

    def __init__(self, distance_measure: str = DEFAULT_DISTANCE_MEASURE) -> None:
        self._store: dict[str, VectorRecord] = {}
        self.distance_measure = distance_measure

    def put(self, record: VectorRecord) -> None:
        self.put_many((record,))

    def put_many(self, records: Iterable[VectorRecord]) -> None:
        unique = _deduplicate_records(records)
        for record in unique:
            key = _record_doc_id(record)
            existing = self._store.get(key)
            if existing is not None and _record_fingerprint(
                existing
            ) != _record_fingerprint(record):
                raise ValueError(f"conflicting records for document id {key!r}")
        for record in unique:
            key = _record_doc_id(record)
            if key not in self._store:
                self._store[key] = record

    def _distance(self, a: list[float], b: list[float]) -> float:
        if self.distance_measure == "EUCLIDEAN":
            return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=False)))
        if self.distance_measure == "DOT_PRODUCT":
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            return 1.0 - dot
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 1.0
        cos = dot / (norm_a * norm_b)
        return 1.0 - cos

    def find_nearest(
        self,
        query_vector: list[float],
        *,
        limit: int = 5,
        distance_threshold: float | None = None,
        filter_source_kind: str | None = None,
        distance_result_field: str = DEFAULT_DISTANCE_RESULT_FIELD,
        **kwargs: Any,
    ) -> list[tuple[VectorRecord, float]] | list[NearestResult]:
        real_limit = kwargs.get("limit", limit)
        real_threshold = kwargs.get("distance_threshold", distance_threshold)
        src_kind = kwargs.get("source_kind", filter_source_kind)
        scored: list[tuple[VectorRecord, float]] = []
        for rec in self._store.values():
            if (
                src_kind
                and rec.source_kind != src_kind
                and rec.metadata.get("source_kind") != src_kind
            ):
                continue
            vec = rec.vector_as_list()
            if len(vec) != len(query_vector):
                n = min(len(vec), len(query_vector))
                vec = vec[:n]
                qv = query_vector[:n]
            else:
                qv = query_vector
            d = self._distance(vec, qv)
            if real_threshold is not None and d > real_threshold:
                continue
            scored.append((rec, d))
        scored.sort(key=lambda x: x[1])
        limited = scored[:real_limit]
        return limited

    def clear(self) -> None:
        self._store.clear()

    def count(self) -> int:
        return len(self._store)

    def find_nearest_as_results(
        self,
        query_vector: list[float],
        *,
        limit: int = 10,
        distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
        source_kind: str | None = None,
        edition: str | None = None,
    ) -> list[NearestResult]:
        raw = self.find_nearest(
            query_vector,
            limit=limit,
            distance_threshold=distance_threshold,
            filter_source_kind=source_kind,
        )
        out: list[NearestResult] = []
        for rec, dist in raw:  # type: ignore[misc]
            dot = 1.0 - dist if self.distance_measure == "DOT_PRODUCT" else 1.0 - dist
            if dot < distance_threshold:
                continue
            out.append(
                NearestResult(
                    record=rec, raw=rec.to_firestore_dict(), distance=dot, is_match=True
                )
            )
        out.sort(key=lambda r: r.distance or -1, reverse=True)
        return out
