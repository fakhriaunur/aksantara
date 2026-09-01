"""Firestore vector store — split facade preserving public contract."""

from __future__ import annotations

from aksantara.embeddings.firestore_client import FirestoreVectorStore
from aksantara.embeddings.firestore_memory import EmbeddingStore, InMemoryVectorStore
from aksantara.embeddings.firestore_types import (
    CONFIG_COLLECTION,
    CURRENT_VERSION_DOC,
    DEFAULT_DIMS,
    DEFAULT_DISTANCE_MEASURE,
    DEFAULT_DISTANCE_RESULT_FIELD,
    DEFAULT_DISTANCE_THRESHOLD,
    ENTRIES_COLLECTION,
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

__all__ = [
    "CONFIG_COLLECTION",
    "CURRENT_VERSION_DOC",
    "DEFAULT_DIMS",
    "DEFAULT_DISTANCE_MEASURE",
    "DEFAULT_DISTANCE_RESULT_FIELD",
    "DEFAULT_DISTANCE_THRESHOLD",
    "ENTRIES_COLLECTION",
    "FIRESTORE_BATCH_LIMIT",
    "VECTOR_COLLECTION",
    "VECTOR_FIELD",
    "EmbeddingStore",
    "FirestoreVectorStore",
    "InMemoryVectorStore",
    "NearestResult",
    "VectorRecord",
    "_deduplicate_records",
    "_record_doc_id",
    "_record_fingerprint",
    "_record_payload",
    "_unwrap_vector",
    "_wrap_vector",
]
