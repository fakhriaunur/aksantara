"""VectorRecord and Firestore constants for split adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

DEFAULT_DIMS: int = 768
DEFAULT_DISTANCE_MEASURE: str = "DOT_PRODUCT"
DEFAULT_DISTANCE_THRESHOLD: float = 0.70
DEFAULT_DISTANCE_RESULT_FIELD: str = "vector_distance"
FIRESTORE_BATCH_LIMIT: int = 500
VECTOR_FIELD: str = "embedding_vector"
VECTOR_COLLECTION: str = "vector_entries"
ENTRIES_COLLECTION: str = "entries"
CONFIG_COLLECTION: str = "config"
CURRENT_VERSION_DOC: str = "current_version"

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
    "VectorRecord",
    "NearestResult",
]


@dataclass
class VectorRecord:
    """Immutable record written to ``vector_entries``.

    Flexible constructor supports both shapes:
    - Spec: VectorRecord(id='februari', version='2026-08-30.1', lema='Februari', embedding=(...), model=..., dimensions=768, content_hash=..., source_kind=..., edition=...)
    - Legacy: VectorRecord(entry_id='februari', vector=[...], metadata={...}, lema='Februari')
    """

    id: str = ""  # type: ignore[assignment]
    version: str = ""
    lema: str = ""
    embedding: tuple[float, ...] = field(default_factory=tuple)  # type: ignore[assignment]
    model: str = "gemini-embedding-001"
    dimensions: int = DEFAULT_DIMS
    content_hash: str = ""
    source_kind: str = "official-live"
    edition: str = "VI"
    source_version: str = "VI"
    parser_version: str = "0.1.0"
    embedding_document: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    entry_id: str = ""
    vector: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.entry_id and not self.id:
            object.__setattr__(self, "id", self.entry_id)
        if self.vector and not self.embedding:
            object.__setattr__(self, "embedding", tuple(float(x) for x in self.vector))
        if self.id and not self.entry_id:
            object.__setattr__(self, "entry_id", self.id)
        if self.embedding and not self.vector:
            object.__setattr__(self, "vector", list(self.embedding))
        if self.metadata and not self.content_hash:
            ch = (
                self.metadata.get("content_hash")
                or self.metadata.get("contentHash")
                or ""
            )
            if ch:
                object.__setattr__(self, "content_hash", ch)
        if self.content_hash and not self.metadata.get("content_hash"):
            md = dict(self.metadata)
            md["content_hash"] = self.content_hash
            object.__setattr__(self, "metadata", md)
        if self.source_kind and not self.metadata.get("source_kind"):
            md2 = dict(self.metadata)
            md2["source_kind"] = self.source_kind
            object.__setattr__(self, "metadata", md2)
        if (
            self.dimensions == DEFAULT_DIMS
            and self.vector
            and len(self.vector) != DEFAULT_DIMS
            and not self.embedding
        ):
            object.__setattr__(self, "dimensions", len(self.vector))

    @property
    def doc_id(self) -> str:
        if self.version:
            return f"{self.id}_{self.version}"
        return self.entry_id or self.id

    def to_firestore_dict(self) -> dict[str, Any]:
        return {
            "id": self.id or self.entry_id,
            "entry_id": self.entry_id or self.id,
            "version": self.version,
            "lema": self.lema,
            VECTOR_FIELD: list(self.embedding) if self.embedding else list(self.vector),
            "embedding": list(self.vector) if self.vector else list(self.embedding),
            "model": self.model,
            "dimensions": self.dimensions
            or (len(self.vector) if self.vector else DEFAULT_DIMS),
            "contentHash": self.content_hash,
            "content_hash": self.content_hash,
            "source_kind": self.source_kind,
            "edition": self.edition,
            "source_version": self.source_version,
            "parser_version": self.parser_version,
            "embedding_document": self.embedding_document,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": datetime.now(UTC),
        }

    def vector_as_list(self) -> list[float]:
        if self.embedding:
            return list(self.embedding)
        return list(self.vector)


@dataclass(frozen=True, slots=True)
class NearestResult:
    record: VectorRecord | None
    raw: dict[str, Any] | None = None
    distance: float | None = None
    is_match: bool = False
