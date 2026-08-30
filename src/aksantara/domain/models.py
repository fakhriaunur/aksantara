"""Canonical domain models for Aksantara.

Strict pydantic v2 models with no silent coercion. All lexical facts
originate from SourceRef; KBBIEntry carries full provenance and version
pins so every record is replayable and rollback-safe.

Determinism contract:
- Use `model_dump(mode="json")` for canonical serialization.
- `makna` list order is source order; parsers must not reorder.
- Hash-then-compare via `aksantara.domain.provenance` for replay gates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class SourceRef(BaseModel):
    """Provenance pointer for a single KBBI snapshot."""

    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, str_strip_whitespace=True
    )

    url: Annotated[
        str,
        Field(
            description="Canonical source URL, e.g. kbbi.kemdikbud.go.id/entri/februari"
        ),
    ]
    source_kind: Annotated[
        Literal[
            "official-live",
            "official-snapshot",
            "rule",
            "sipebi",
            "gov-derived",
            "fallback",
            "enrichment",
            "evaluation",
            "ai-proposal",
        ],
        Field(description="Storage-facing source kind; Firestore pre-filter uses this"),
    ] = "official-live"
    edition: Annotated[str, Field(description="KBBI edition, e.g. VI")] = "VI"
    source_version: Annotated[
        str, Field(description="Source version tag or edition snapshot date")
    ] = "VI"
    retrieved_at: Annotated[
        datetime, Field(description="UTC timestamp when raw snapshot was fetched")
    ]
    content_hash: Annotated[
        str,
        Field(
            description="Hex sha256 of raw snapshot bytes (lower-case, 64 chars)",
            min_length=64,
            max_length=64,
            pattern=r"^[0-9a-f]{64}$",
        ),
    ]
    parser_version: Annotated[
        str, Field(description="Parser version that produced this record")
    ] = "0.1.0"

    @field_validator("retrieved_at")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)

    @field_validator("content_hash")
    @classmethod
    def _lower_hash(cls, v: str) -> str:
        return v.lower()


# ---------------------------------------------------------------------------
# Aggregate root
# ---------------------------------------------------------------------------

Status = Literal["active", "inactive", "unresolved"]
ReviewStatus = Literal["pending", "approved", "rejected", "quarantined"]


class KBBIEntry(BaseModel):
    """Canonical KBBI entry — aggregate root.

    Every field maps 1:1 to the KBBI VI Daring source contract. Optional
    fields are None when absent in source; parsers must not invent values.
    `makna` is the authoritative sense list; `contoh` aligns by index when
    present.
    """

    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    # Identity
    id: Annotated[
        str, Field(description="Stable entry id, typically lema lower-cased slug")
    ]
    lema: Annotated[str, Field(description="Headword as shown in KBBI", min_length=1)]
    sub_lema: Annotated[
        list[str], Field(description="Sub-lemmas / variant headwords")
    ] = Field(default_factory=list)
    ejaan: Annotated[
        str | None, Field(description="Canonical orthography if distinct from lema")
    ] = None

    # Lexical content
    kelas_kata: Annotated[
        list[str], Field(description="Word-class labels, e.g. n, v, a")
    ] = Field(default_factory=list)
    makna: Annotated[
        list[dict[str, Any]],
        Field(
            description="Sense list; each dict has at least 'definisi' or 'makna'; order is source order"
        ),
    ]
    contoh: Annotated[
        list[str], Field(description="Usage examples aligned to makna where possible")
    ] = Field(default_factory=list)
    turunan: Annotated[
        list[str], Field(description="Derived forms listed under entry")
    ] = Field(default_factory=list)
    bentuk_baku: Annotated[
        str | None,
        Field(description="Standard form if this entry is nonstandard variant"),
    ] = None
    bentuk_tidak_baku: Annotated[
        list[str],
        Field(description="Nonstandard variants that point to the standard form"),
    ] = Field(default_factory=list)
    pelafalan: Annotated[str | None, Field(description="Pronunciation guide")] = None
    pemenggalan: Annotated[str | None, Field(description="Syllabification")] = None
    etimologi: Annotated[str | None, Field(description="Etymology note")] = None
    labels: Annotated[
        list[str], Field(description="Domain/register labels, e.g. Ark, Kas")
    ] = Field(default_factory=list)

    # Lifecycle
    status: Annotated[Status, Field(description="active | inactive | unresolved")] = (
        "active"
    )

    # Provenance & versioning
    source: Annotated[
        SourceRef, Field(description="Provenance pointer for this record")
    ]
    parser_version: Annotated[
        str, Field(description="Parser version that emitted this record")
    ] = "0.1.0"
    transform_version: Annotated[
        str, Field(description="Transform/normalization version")
    ] = "0.1.0"
    review_status: Annotated[
        ReviewStatus, Field(description="Human review gate status")
    ] = "pending"
    confidence: Annotated[
        float, Field(description="Parser confidence 0.0-1.0", ge=0.0, le=1.0)
    ] = 1.0

    @field_validator("makna")
    @classmethod
    def _validate_makna(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(v) == 0:
            raise ValueError("makna must contain at least one sense")
        for idx, sense in enumerate(v):
            if not isinstance(sense, dict):
                raise ValueError(f"makna[{idx}] must be a dict")
            if not any(
                k in sense for k in ("definisi", "makna", "arti", "sense", "definition")
            ):
                raise ValueError(
                    f"makna[{idx}] must contain one of definisi/makna/arti/sense/definition, got keys={sorted(sense.keys())}"
                )
        return v

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        # Pydantic already enforces ge/le, this keeps strict + explicit.
        return v


__all__ = ["KBBIEntry", "ReviewStatus", "SourceRef", "Status"]
