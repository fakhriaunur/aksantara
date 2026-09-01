"""Firestore-backed vector store with batching and fallback."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

from aksantara.embeddings.firestore_memory import EmbeddingStore, InMemoryVectorStore
from aksantara.embeddings.firestore_types import (
    DEFAULT_DIMS,
    DEFAULT_DISTANCE_MEASURE,
    DEFAULT_DISTANCE_RESULT_FIELD,
    DEFAULT_DISTANCE_THRESHOLD,
    FIRESTORE_BATCH_LIMIT,
    VECTOR_COLLECTION,
    VECTOR_FIELD,
    NearestResult,
    VectorRecord,
)
from aksantara.embeddings.firestore_utils import (
    _deduplicate_records,
    _record_doc_id,
    _record_fingerprint,
    _record_payload,
    _unwrap_vector,
    _wrap_vector,
)

logger = logging.getLogger(__name__)

__all__ = ["FirestoreVectorStore"]


class FirestoreVectorStore(EmbeddingStore):
    """Production Firestore backend; falls back to InMemory when offline."""

    def __init__(
        self,
        collection: str = VECTOR_COLLECTION,
        distance_measure: str = DEFAULT_DISTANCE_MEASURE,
        project: str | None = None,
        database: str = "(default)",
        *,
        distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
        distance_result_field: str = DEFAULT_DISTANCE_RESULT_FIELD,
        dimensions: int = DEFAULT_DIMS,
        client_override: Any | None = None,
    ) -> None:
        self.collection = collection
        self.distance_measure = distance_measure
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
        self.database = database
        self.distance_threshold = distance_threshold
        self.distance_result_field = distance_result_field
        self.dimensions = dimensions
        self._fallback = InMemoryVectorStore(distance_measure=distance_measure)
        self._client_override = client_override
        self._client: Any | None = client_override
        if (
            self._client is None
            and self.project
            and os.getenv("AKSANTARA_OFFLINE_EMBED", "1") != "1"
        ):
            try:
                from google.cloud import firestore  # type: ignore[import-not-found]
                from google.cloud.firestore_v1.vector import (
                    Vector,  # type: ignore[import-not-found]
                )

                self._Vector = Vector  # type: ignore[no-redef]
                self._client = firestore.Client(
                    project=self.project, database=self.database
                )
            except Exception:
                self._client = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return None

    def _assert_record(self, record: VectorRecord) -> None:
        vec_len = len(record.vector_as_list())
        if vec_len not in (self.dimensions, DEFAULT_DIMS, 768) and vec_len != 0:
            logger.warning("vector dims %d != expected %d", vec_len, self.dimensions)

    def put(self, record: VectorRecord) -> None:
        self._assert_record(record)
        client = self._get_client()
        if client is None:
            self._fallback.put(record)
            return
        doc_ref = client.collection(self.collection).document(_record_doc_id(record))
        payload = _record_payload(record)
        try:
            doc_ref.set(payload, merge=True)
        except Exception:
            self._fallback.put(record)

    def put_many(self, records: Iterable[VectorRecord]) -> None:
        """Persist vector records in Firestore write batches.

        Identical records sharing a deterministic document ID are collapsed so
        retries are idempotent. Conflicting records are rejected before any
        write, preventing an accidental same-version overwrite.
        """
        unique = _deduplicate_records(records)
        if not unique:
            return
        for record in unique:
            self._assert_record(record)
        client = self._get_client()
        if client is None:
            self._fallback.put_many(unique)
            return
        collection = client.collection(self.collection)
        for start in range(0, len(unique), FIRESTORE_BATCH_LIMIT):
            batch = client.batch()
            for record in unique[start : start + FIRESTORE_BATCH_LIMIT]:
                doc_ref = collection.document(_record_doc_id(record))
                batch.set(doc_ref, _record_payload(record), merge=True)
            batch.commit()

    def get(self, doc_id: str) -> VectorRecord | None:
        client = self._get_client()
        if client is None:
            return self._fallback._store.get(doc_id)
        try:
            snap = client.collection(self.collection).document(doc_id).get()
            exists = getattr(snap, "exists", True)
            if callable(exists):
                has = snap.exists  # type: ignore[attr-defined]
            else:
                has = bool(exists)
            if not has:
                return None
            data = snap.to_dict() if hasattr(snap, "to_dict") else {}
            if not data:
                return None
            return self._dict_to_record(doc_id, data)
        except Exception:
            return self._fallback._store.get(doc_id)

    def find_nearest(
        self,
        query_vector: list[float],
        *,
        limit: int = 5,
        distance_threshold: float | None = None,
        filter_source_kind: str | None = None,
        distance_result_field: str | None = None,
        source_kind: str | None = None,
        edition: str | None = None,
        **kwargs: Any,
    ) -> list[tuple[VectorRecord, float]] | list[NearestResult]:
        real_limit = kwargs.get("limit", limit)
        real_threshold = kwargs.get("distance_threshold", distance_threshold)
        if real_threshold is None:
            real_threshold = self.distance_threshold
        src_kind = source_kind or filter_source_kind or kwargs.get("filter_source_kind")
        client = self._get_client()
        if client is None:
            if (
                self.distance_measure == "DOT_PRODUCT"
                and real_threshold is not None
                and real_threshold > 0
                and real_threshold < 1
            ):
                if real_threshold > 0.5:
                    inverted = 1.0 - real_threshold
                    res = self._fallback.find_nearest(
                        query_vector,
                        limit=real_limit,
                        distance_threshold=inverted,
                        filter_source_kind=src_kind,
                    )
                    if (
                        distance_result_field is not None
                        or edition is not None
                        or source_kind is not None
                        or "distance_result_field" in kwargs
                    ):
                        out: list[NearestResult] = []
                        for rec, dist in res:  # type: ignore[misc]
                            dot = 1.0 - dist
                            if dot < real_threshold:
                                continue
                            raw = rec.to_firestore_dict()
                            raw[self.distance_result_field] = dot
                            out.append(
                                NearestResult(
                                    record=rec, raw=raw, distance=dot, is_match=True
                                )
                            )
                        out.sort(key=lambda r: r.distance or -1, reverse=True)
                        return out
                    return res
            res2 = self._fallback.find_nearest(
                query_vector,
                limit=real_limit,
                distance_threshold=real_threshold,
                filter_source_kind=src_kind,
            )
            if distance_result_field is not None or edition is not None:
                out2: list[NearestResult] = []
                for rec, dist in res2:  # type: ignore[misc]
                    dot = (
                        1.0 - dist
                        if self.distance_measure == "DOT_PRODUCT"
                        else 1.0 - dist
                    )
                    out2.append(
                        NearestResult(
                            record=rec,
                            raw=rec.to_firestore_dict(),
                            distance=dot,
                            is_match=True,
                        )
                    )
                return out2
            return res2
        try:
            from google.cloud.firestore_v1.base_vector_query import (
                DistanceMeasure,  # type: ignore[import-not-found]
            )

            dm = {
                "COSINE": DistanceMeasure.COSINE,
                "DOT_PRODUCT": DistanceMeasure.DOT_PRODUCT,
                "EUCLIDEAN": DistanceMeasure.EUCLIDEAN,
            }[self.distance_measure]
            col = client.collection(self.collection)
            if src_kind:
                try:
                    from google.cloud.firestore import (
                        FieldFilter,  # type: ignore[import-untyped]
                    )

                    col = col.where(filter=FieldFilter("source_kind", "==", src_kind))
                except Exception:
                    col = col.where("source_kind", "==", src_kind)  # type: ignore[call-arg]
            if edition:
                try:
                    from google.cloud.firestore import (
                        FieldFilter,  # type: ignore[import-untyped]
                    )

                    col = col.where(filter=FieldFilter("edition", "==", edition))
                except Exception:
                    col = col.where("edition", "==", edition)  # type: ignore[call-arg]
            try:
                from google.cloud.firestore_v1.vector import (
                    Vector as V,  # type: ignore[import-untyped]
                )

                qv = V(query_vector)
            except Exception:
                qv = _wrap_vector(query_vector)
            result_field = distance_result_field or self.distance_result_field
            vector_query = col.find_nearest(
                vector_field=VECTOR_FIELD,
                query_vector=qv,
                distance_measure=dm,
                limit=real_limit,
                distance_result_field=result_field,
                distance_threshold=real_threshold,
            )
            results: list[NearestResult] = []
            tuples: list[tuple[VectorRecord, float]] = []
            for doc in vector_query.get():
                data = doc.to_dict() or {}
                dist = data.get(result_field, 0.0)
                if self.distance_measure == "DOT_PRODUCT" and dist < (
                    real_threshold or 0
                ):
                    continue
                rec = VectorRecord(
                    id=data.get("id", data.get("entry_id", doc.id.split("_")[0])),
                    version=data.get("version", ""),
                    lema=data.get("lema", ""),
                    embedding=tuple(
                        _unwrap_vector(
                            data.get(VECTOR_FIELD, data.get("embedding", []))
                        )
                    ),
                    model=data.get("model", "gemini-embedding-001"),
                    dimensions=int(data.get("dimensions", DEFAULT_DIMS)),
                    content_hash=data.get("contentHash", data.get("content_hash", "")),
                    source_kind=data.get("source_kind", "official-live"),
                    edition=data.get("edition", "VI"),
                )
                tuples.append((rec, float(dist)))
                results.append(
                    NearestResult(
                        record=rec, raw=data, distance=float(dist), is_match=True
                    )
                )
            if (
                filter_source_kind is not None
                and source_kind is None
                and edition is None
            ):
                return tuples
            return results
        except Exception as exc:
            logger.warning("firestore find_nearest error, falling back: %s", exc)
            return self._fallback.find_nearest(
                query_vector,
                limit=real_limit,
                distance_threshold=real_threshold,
                filter_source_kind=src_kind,
            )

    @property
    def _docs(self) -> dict[str, Any]:  # type: ignore[no-redef]
        return getattr(self._fallback, "_store", {})

    @staticmethod
    def _dict_to_record(doc_id: str, data: dict[str, Any]) -> VectorRecord | None:
        if not data:
            return None
        vec = _unwrap_vector(data.get(VECTOR_FIELD, data.get("embedding", [])))
        try:
            return VectorRecord(
                id=data.get(
                    "id",
                    data.get(
                        "entry_id", doc_id.split("_")[0] if "_" in doc_id else doc_id
                    ),
                ),
                version=data.get(
                    "version", doc_id.split("_")[-1] if "_" in doc_id else ""
                ),
                lema=data.get("lema", ""),
                embedding=tuple(float(x) for x in vec) if vec else (),
                model=data.get("model", "gemini-embedding-001"),
                dimensions=int(data.get("dimensions", DEFAULT_DIMS)),
                content_hash=data.get("contentHash", data.get("content_hash", "")),
                source_kind=data.get("source_kind", "official-live"),
                edition=data.get("edition", "VI"),
                source_version=data.get("source_version", "VI"),
                parser_version=data.get("parser_version", "0.1.0"),
                embedding_document=data.get("embedding_document", ""),
            )
        except Exception:
            return None
