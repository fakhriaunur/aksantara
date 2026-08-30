"""Quarantine writer — review_queue in-memory for Phase 2, Firestore later.

Thread-safe in-memory store. Each quarantined entry is preserved with
reason, details, and snapshot for human review.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aksantara.domain.errors import QuarantinedError
from aksantara.domain.models import KBBIEntry

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class QuarantineRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    entry_id: str = Field(description="Entry id or lema that triggered quarantine")
    lema: str | None = Field(default=None, description="Headword if known")
    source_kind: str = Field(description="Source kind of quarantined record")
    content_hash: str | None = Field(default=None, description="Content hash if known")
    reason: str = Field(description="Machine-readable quarantine code")
    details: str | None = Field(default=None, description="Human-readable context")
    quarantined_at: datetime = Field(description="UTC timestamp of quarantine")
    entry_snapshot: dict[str, Any] | None = Field(
        default=None, description="Canonical JSON snapshot of entry if available"
    )
    review_status: str = Field(
        default="quarantined", description="Review status, always quarantined on write"
    )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# In-memory store (Phase 2)
# ---------------------------------------------------------------------------

_lock: threading.Lock = threading.Lock()
_REVIEW_QUEUE: list[QuarantineRecord] = []


def quarantine_entry(
    entry: KBBIEntry | dict[str, Any] | None,
    reason: str,
    *,
    details: str | None = None,
    source_kind: str | None = None,
    entry_id: str | None = None,
    content_hash: str | None = None,
) -> QuarantineRecord:
    """Add entry to review_queue and return record.

    Args:
        entry: KBBIEntry or dict snapshot, or None if only metadata known.
        reason: machine-readable code.
        details: optional human context.
        source_kind: override source_kind if not inferrable from entry.
        entry_id: override id if entry is None.
        content_hash: override hash if entry is None.

    Returns:
        QuarantineRecord appended to queue.

    Note:
        Phase 2: in-memory only. Firestore integration will mirror this
        record to `review_queue/{entry_id}_{hash}_{timestamp}` later.
        TODO(Firestore): add firestore.Client write behind feature flag.
    """
    # Extract fields from entry if provided
    lema_val: str | None = None
    sid: str | None = entry_id
    sk: str | None = source_kind
    ch: str | None = content_hash
    snapshot: dict[str, Any] | None = None

    if isinstance(entry, KBBIEntry):
        lema_val = entry.lema
        sid = sid or entry.id
        sk = sk or entry.source.source_kind
        ch = ch or entry.source.content_hash
        snapshot = entry.model_dump(mode="json")
    elif isinstance(entry, dict):
        # try to infer
        lema_val = entry.get("lema") if isinstance(entry.get("lema"), str) else None
        sid = sid or (entry.get("id") if isinstance(entry.get("id"), str) else None)
        # source may be nested
        src: Any = entry.get("source")
        if isinstance(src, dict):
            sk = sk or (
                src.get("source_kind")
                if isinstance(src.get("source_kind"), str)
                else None
            )
            ch = ch or (
                src.get("content_hash")
                if isinstance(src.get("content_hash"), str)
                else None
            )
            if not ch:
                ch = (
                    src.get("contentHash")
                    if isinstance(src.get("contentHash"), str)
                    else None
                )
        snapshot = entry
    else:
        # entry is None
        pass

    sid = sid or "unknown"
    sk = sk or "unknown"

    rec: QuarantineRecord = QuarantineRecord(
        entry_id=sid,
        lema=lema_val,
        source_kind=sk,
        content_hash=ch,
        reason=reason,
        details=details,
        quarantined_at=datetime.now(UTC),
        entry_snapshot=snapshot,
        review_status="quarantined",
    )
    with _lock:
        _REVIEW_QUEUE.append(rec)
    # TODO(Firestore): if firestore client available and env flag, also write to review_queue collection
    return rec


def quarantine_from_error(
    error: QuarantinedError,
    entry: KBBIEntry | dict[str, Any] | None = None,
) -> QuarantineRecord:
    """Create quarantine record from a QuarantinedError."""
    return quarantine_entry(
        entry,
        reason=error.reason,
        details=error.details,
        source_kind=error.source_kind,
        entry_id=error.entry_id,
    )


# Alias for spec compatibility
quarantine = quarantine_entry


def get_review_queue() -> list[QuarantineRecord]:
    """Return shallow copy of review queue (deterministic order = insertion order)."""
    with _lock:
        return list(_REVIEW_QUEUE)


def clear_review_queue() -> None:
    """Clear in-memory queue (for tests)."""
    with _lock:
        _REVIEW_QUEUE.clear()


def is_quarantined(entry_id: str) -> bool:
    with _lock:
        return any(r.entry_id == entry_id for r in _REVIEW_QUEUE)


def queue_size() -> int:
    with _lock:
        return len(_REVIEW_QUEUE)


__all__ = [
    "QuarantineRecord",
    "QuarantineStore",
    "clear_review_queue",
    "get_review_queue",
    "is_quarantined",
    "quarantine",
    "quarantine_entry",
    "quarantine_from_error",
    "queue_size",
]


class QuarantineStore:
    """Back-compat wrapper around module-level queue."""

    def quarantine(self, error, entry=None, raw_hash=None):  # type: ignore[no-untyped-def]
        from aksantara.domain.errors import QuarantinedError as _QE

        if isinstance(error, _QE):
            return quarantine_from_error(error, entry)
        return quarantine_entry(
            entry, str(error), details=getattr(error, "details", None)
        )

    def list(self):  # type: ignore[no-untyped-def]
        return get_review_queue()

    def count(self) -> int:
        return queue_size()

    def clear(self) -> None:
        clear_review_queue()

    def to_dicts(self):  # type: ignore[no-untyped-def]
        return [r.to_dict() for r in get_review_queue()]
