"""Conflict detection — diff_versions and authority-aware quarantine.

Pure functions. Compares old vs new KBBIEntry field-by-field and quarantines
if direct (official) vs fallback mismatch on substantive fields.
"""

from __future__ import annotations

from typing import Any

from aksantara.domain.errors import QuarantinedError
from aksantara.domain.models import KBBIEntry
from aksantara.domain.provenance import canonical_json_hash

OFFICIAL_KINDS: frozenset[str] = frozenset({"official-live", "official-snapshot"})
FALLBACK_KINDS: frozenset[str] = frozenset({"fallback", "gov-derived"})

# Fields considered substantive for quarantine when official vs fallback diverge
SUBSTANTIVE_FIELDS: tuple[str, ...] = (
    "lema",
    "makna",
    "kelas_kata",
    "contoh",
    "bentuk_baku",
    "bentuk_tidak_baku",
    "etimologi",
    "status",
)


def _field_value(entry: KBBIEntry, field: str) -> Any:
    # Use getattr; for source fields we compare separately if needed
    return getattr(entry, field, None)


def _values_equal(a: Any, b: Any) -> bool:
    # Deterministic deep equality via canonical_json_hash for complex types
    if a is b:
        return True
    if type(a) is not type(b):
        # allow list vs tuple comparison via hash of JSON
        try:
            ha: str = canonical_json_hash(
                a if isinstance(a, (dict, list)) else {"v": a}  # type: ignore
            )
            hb: str = canonical_json_hash(
                b if isinstance(b, (dict, list)) else {"v": b}  # type: ignore
            )
            return bool(ha == hb)
        except Exception:
            return bool(a == b)
    if isinstance(a, (dict, list)):
        try:
            return bool(canonical_json_hash(a) == canonical_json_hash(b))  # type: ignore
        except Exception:
            return bool(a == b)
    return bool(a == b)


def diff_versions(old: KBBIEntry, new: KBBIEntry) -> dict[str, tuple[Any, Any]]:
    """Return dict of field -> (old_value, new_value) where values differ.

    Compares substantive fields plus source provenance (contentHash, source_kind).
    Does not mutate inputs. Deterministic: iterates SUBSTANTIVE_FIELDS in fixed order
    then checks provenance.

    Raises:
        QuarantinedError if direct vs fallback substantive mismatch (fail-closed).
        The diff is still available in the exception details.

    Returns:
        Diff dict (may be empty if no changes).
    """
    diffs: dict[str, tuple[Any, Any]] = {}

    for field in SUBSTANTIVE_FIELDS:
        ov: Any = _field_value(old, field)
        nv: Any = _field_value(new, field)
        if not _values_equal(ov, nv):
            diffs[field] = (ov, nv)

    # Also track provenance changes that affect replay
    if old.source.content_hash != new.source.content_hash:
        diffs["source.content_hash"] = (
            old.source.content_hash,
            new.source.content_hash,
        )
    if old.source.source_kind != new.source.source_kind:
        diffs["source.source_kind"] = (old.source.source_kind, new.source.source_kind)
    if old.source.parser_version != new.source.parser_version:
        diffs["source.parser_version"] = (
            old.source.parser_version,
            new.source.parser_version,
        )

    # Authority-aware quarantine: if old is official and new is fallback (or vice versa)
    # and substantive diffs exist, quarantine to prevent relabeling.
    old_kind: str = old.source.source_kind
    new_kind: str = new.source.source_kind
    is_direct_fallback_mismatch: bool = (
        old_kind in OFFICIAL_KINDS and new_kind in FALLBACK_KINDS
    ) or (old_kind in FALLBACK_KINDS and new_kind in OFFICIAL_KINDS)
    if is_direct_fallback_mismatch:
        # Check if any substantive field differs
        substantive_conflicts: dict[str, tuple[Any, Any]] = {
            k: v for k, v in diffs.items() if k in SUBSTANTIVE_FIELDS
        }
        if substantive_conflicts:
            # Build deterministic details string sorted by field name
            details_parts: list[str] = []
            for k in sorted(substantive_conflicts.keys()):
                ov, nv = substantive_conflicts[k]
                # Use repr but truncate large makna
                ov_r: str = repr(ov)[:500]
                nv_r: str = repr(nv)[:500]
                details_parts.append(f"{k}: {ov_r} != {nv_r}")
            details: str = "; ".join(details_parts)
            raise QuarantinedError(
                reason="direct_vs_fallback_mismatch",
                entry_id=new.id,
                source_kind=new_kind,
                details=f"official vs fallback substantive conflict on {', '.join(sorted(substantive_conflicts.keys()))}: {details}",
            )

    return diffs


def detect_conflicts(old: KBBIEntry, new: KBBIEntry) -> dict[str, tuple[Any, Any]]:
    """Alias for diff_versions — explicit conflict detection entry point."""
    return diff_versions(old, new)


def has_substantive_conflict(old: KBBIEntry, new: KBBIEntry) -> bool:
    """Return True if official vs fallback substantive conflict exists without raising."""
    try:
        diff_versions(old, new)
        return False
    except QuarantinedError:
        return True


__all__ = [
    "FALLBACK_KINDS",
    "OFFICIAL_KINDS",
    "SUBSTANTIVE_FIELDS",
    "detect_conflict",
    "detect_conflicts",
    "diff_versions",
    "has_substantive_conflict",
]

# Back-compat alias (singular) for earlier tests
detect_conflict = detect_conflicts
