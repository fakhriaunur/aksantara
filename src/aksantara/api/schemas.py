"""Pydantic response schemas for Aksantara API.

Every citation-bearing response includes provenance fields per
``docs/downstream-contract.md``. Schemas are strict and self-documenting
via OpenAPI; no secret material is ever exposed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceProvenanceSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    url: str = Field(description="Canonical source URL")
    edition: str = Field(description="KBBI edition, e.g. VI")
    source_version: str = Field(description="Source version tag")
    retrievedAt: str = Field(description="UTC ISO-8601 when snapshot was fetched")
    contentHash: str = Field(description="Hex sha256 of raw snapshot (64 chars)")
    parserVersion: str = Field(description="Parser version that emitted the record")


class RetrievalSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    mode: str = Field(description="exact | prefix | semantic | nonstandard")
    distance: float | None = Field(
        default=None, description="DOT_PRODUCT score when applicable"
    )
    threshold: float | None = Field(
        default=None, description="Distance threshold for semantic"
    )
    distance_result_field: str = Field(default="vector_distance")


class CitationSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    entry_id: str
    lema: str
    source: SourceProvenanceSchema
    retrieval: RetrievalSchema


class KBBIEntrySchema(BaseModel):
    """API-facing projection of KBBIEntry — read-only."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: str
    lema: str
    sub_lema: list[str] = Field(default_factory=list)
    ejaan: str | None = None
    kelas_kata: list[str] = Field(default_factory=list)
    makna: list[dict[str, Any]]
    contoh: list[str] = Field(default_factory=list)
    turunan: list[str] = Field(default_factory=list)
    bentuk_baku: str | None = None
    bentuk_tidak_baku: list[str] = Field(default_factory=list)
    pelafalan: str | None = None
    pemenggalan: str | None = None
    etimologi: str | None = None
    labels: list[str] = Field(default_factory=list)
    status: str = "active"
    source: SourceProvenanceSchema
    parser_version: str = "0.1.0"
    transform_version: str = "0.1.0"
    review_status: str = "pending"
    confidence: float = 1.0
    citation: CitationSchema | None = None
    retrieval: RetrievalSchema | None = None


class EntryResultSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    entry: KBBIEntrySchema
    citation: CitationSchema
    retrieval: RetrievalSchema
    # Legacy alias for consumers that read distance at top-level of result.
    vector_distance: float | None = None


class EntriesResponseSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    query: str | None = None
    count: int
    results: list[EntryResultSchema]


class SemanticSearchResponseSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    query: str
    count: int
    results: list[EntryResultSchema]


class VersionResponseSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    version: str
    created_at: str
    edition: str
    entries_count: int
    embedding: dict[str, Any] | None = None
    source: dict[str, Any] | None = None


class HealthResponseSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    status: str = "ok"
    version: str = "0.1.0"
    firestore: str = "unknown"
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class NonStandardRelationSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    word: str
    standard_form: str | None
    variants: list[str] = Field(default_factory=list)
    entry: KBBIEntrySchema | None = None
    citation: CitationSchema | None = None
