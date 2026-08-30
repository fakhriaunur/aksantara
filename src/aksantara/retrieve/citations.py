"""Citation renderer for Aksantara retrieval results — spec + legacy compat.

Every API response must include a citation with:
- source {url, edition, source_version, retrievedAt, contentHash, parserVersion, sourceKind}
- retrieval {mode, distance, threshold, distance_result_field}

Supports both the spec signature (entry, retrieval=RetrievalInfo, mode=, distance=)
and the legacy (entry, mode, distance) positional form.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aksantara.domain.models import KBBIEntry

__all__ = ["RetrievalInfo", "render_citation"]

DEFAULT_DISTANCE_RESULT_FIELD: str = "vector_distance"
DEFAULT_THRESHOLD: float = 0.70


class RetrievalInfo:
    """Lightweight citation value object."""

    def __init__(
        self,
        mode: str,
        *,
        distance: float | None = None,
        threshold: float | None = None,
        distance_result_field: str = DEFAULT_DISTANCE_RESULT_FIELD,
    ) -> None:
        self.mode = mode
        self.distance = distance
        self.threshold = threshold
        self.distance_result_field = distance_result_field

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "mode": self.mode,
            "distance_result_field": self.distance_result_field,
        }
        if self.distance is not None:
            d["distance"] = self.distance
        if self.threshold is not None:
            d["threshold"] = self.threshold
        return d


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def render_citation(
    entry: KBBIEntry,
    mode: str | None = None,
    distance: float | None = None,
    *,
    retrieval: RetrievalInfo | dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Render a citation dict for an entry.

    Flexible signature:
    - Spec: render_citation(entry, retrieval=RetrievalInfo(mode='exact'))
    - Spec legacy: render_citation(entry, retrieval={'mode':'semantic'})
    - Legacy: render_citation(entry, 'exact', 0.9)
    - Legacy kwargs: render_citation(entry, mode='semantic', distance=0.8)
    """
    # Handle positional second arg being a RetrievalInfo/dict
    if mode is not None and isinstance(mode, (RetrievalInfo, dict)):
        retrieval = mode  # type: ignore[assignment]
        mode = None

    # Allow retrieval passed positionally as second arg named retrieval
    if retrieval is None and kwargs.get("retrieval") is not None:
        retrieval = kwargs.pop("retrieval")

    # Resolve mode/distance from legacy positional or kwargs
    real_mode = mode or kwargs.get("mode")
    real_distance = distance if distance is not None else kwargs.get("distance")

    retrieval_block: dict[str, Any]
    if isinstance(retrieval, RetrievalInfo):
        retrieval_block = retrieval.to_dict()
    elif isinstance(retrieval, dict):
        retrieval_block = dict(retrieval)
        # Normalize legacy distance within retrieval dict
        if real_distance is not None and "distance" not in retrieval_block:
            retrieval_block["distance"] = real_distance
        if real_mode is not None and "mode" not in retrieval_block:
            retrieval_block["mode"] = real_mode
    else:
        retrieval_block = {
            "mode": real_mode or "exact",
            "distance_result_field": DEFAULT_DISTANCE_RESULT_FIELD,
        }
        if real_distance is not None:
            retrieval_block["distance"] = real_distance
        if (
            retrieval_block.get("mode") == "semantic"
            and "threshold" not in retrieval_block
        ):
            retrieval_block["threshold"] = DEFAULT_THRESHOLD

    src = entry.source
    # Build source block with both camelCase and snake_case for compat
    source_block = {
        "url": src.url,
        "edition": src.edition,
        "source_version": src.source_version,
        "sourceVersion": src.source_version,
        "sourceKind": src.source_kind,
        "source_kind": src.source_kind,
        "retrievedAt": _iso_utc(src.retrieved_at),
        "retrieved_at": src.retrieved_at.isoformat().replace("+00:00", "Z"),
        "contentHash": src.content_hash,
        "content_hash": src.content_hash,
        "parserVersion": src.parser_version,
        "parser_version": src.parser_version,
    }

    # Legacy also expects makna list and citedAt
    legacy_makna = [
        m.get("definisi") or m.get("makna") or m.get("arti") or "" for m in entry.makna
    ]

    citation: dict[str, Any] = {
        "entryId": entry.id,
        "entry_id": entry.id,
        "lema": entry.lema,
        "makna": legacy_makna,
        "source": source_block,
        "retrieval": retrieval_block,
        "citedAt": datetime.now(UTC).isoformat(),
        "cited_at": datetime.now(UTC).isoformat(),
    }
    # Also include flat aliases for spec tests that read citation['source']['url'] etc
    return citation
