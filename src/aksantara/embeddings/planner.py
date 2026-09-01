"""Content-hash delta planning for incremental embeddings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from aksantara.domain.provenance import canonical_content_payload
from aksantara.embeddings.document import build_embedding_document
from aksantara.embeddings.metadata import (
    EMBEDDING_DIMS,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL,
    EMBEDDING_SCHEMA_VERSION,
    EMBEDDING_TASK_DOCUMENT,
)

__all__ = [
    "DeltaPlan",
    "ExcludedRecord",
    "build_delta_plan",
    "canonical_hash_for_entry",
    "document_hash_for_entry",
]


@dataclass(frozen=True, slots=True)
class ExcludedRecord:
    entry_id: str
    source_kind: str
    reason: str
    no_work: bool = True


@dataclass(frozen=True, slots=True)
class DeltaPlan:
    plan_id: str
    prior_release: str
    candidate_release: str
    prior_ids: list[str]
    candidate_input_ids: list[str]
    eligible_candidate_ids: list[str]
    excluded_ids: list[ExcludedRecord]
    new_ids: list[str]
    changed_ids: list[str]
    unchanged_ids: list[str]
    removed_ids: list[str]
    old_canonical_hash: dict[str, str]
    new_canonical_hash: dict[str, str]
    old_document_hash: dict[str, str]
    new_document_hash: dict[str, str]
    compatible_metadata: dict[str, bool]
    reused_from: dict[str, str]
    origin_release: dict[str, str]
    old_raw_hash: dict[str, str]
    new_raw_hash: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "prior_release": self.prior_release,
            "candidate_release": self.candidate_release,
            "prior_ids": sorted(self.prior_ids),
            "candidate_input_ids": sorted(self.candidate_input_ids),
            "eligible_candidate_ids": sorted(self.eligible_candidate_ids),
            "excluded_ids": [
                {
                    "entry_id": r.entry_id,
                    "source_kind": r.source_kind,
                    "reason": r.reason,
                    "no_work": r.no_work,
                }
                for r in sorted(self.excluded_ids, key=lambda x: x.entry_id)
            ],
            "new": sorted(self.new_ids),
            "changed": sorted(self.changed_ids),
            "unchanged": sorted(self.unchanged_ids),
            "removed": sorted(self.removed_ids),
            "old_canonical_content_hash": self.old_canonical_hash,
            "new_canonical_content_hash": self.new_canonical_hash,
            "old_document_hash": self.old_document_hash,
            "new_document_hash": self.new_document_hash,
            "old_raw_hash": self.old_raw_hash,
            "new_raw_hash": self.new_raw_hash,
            "compatible_metadata": self.compatible_metadata,
            "reused_from": self.reused_from,
            "origin_release": self.origin_release,
            "conservation": {
                "candidate_input_ids_equals_eligible_union_excluded": sorted(
                    set(self.eligible_candidate_ids)
                    | {r.entry_id for r in self.excluded_ids}
                )
                == sorted(self.candidate_input_ids),
                "prior_union_eligible_equals_new_changed_unchanged_removed": sorted(
                    set(self.prior_ids) | set(self.eligible_candidate_ids)
                )
                == sorted(
                    set(self.new_ids)
                    | set(self.changed_ids)
                    | set(self.unchanged_ids)
                    | set(self.removed_ids)
                ),
                "sets_disjoint": len(
                    set(self.new_ids)
                    & set(self.changed_ids)
                    & set(self.unchanged_ids)
                    & set(self.removed_ids)
                )
                == 0
                and len(set(self.new_ids) & set(self.changed_ids)) == 0
                and len(set(self.new_ids) & set(self.unchanged_ids)) == 0
                and len(set(self.new_ids) & set(self.removed_ids)) == 0
                and len(set(self.changed_ids) & set(self.unchanged_ids)) == 0
                and len(set(self.changed_ids) & set(self.removed_ids)) == 0
                and len(set(self.unchanged_ids) & set(self.removed_ids)) == 0,
            },
        }


def canonical_hash_for_entry(entry: Any) -> str:
    """Hash lexical canonical content only, ignoring retrieval timestamps."""
    payload = canonical_content_payload(entry)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def document_hash_for_entry(entry: Any) -> str:
    doc = build_embedding_document(entry)  # type: ignore[arg-type]
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()


def _is_compatible_metadata(vector_meta: dict[str, Any] | None) -> bool:
    if vector_meta is None:
        return False
    try:
        if vector_meta.get("model") != EMBEDDING_MODEL:
            return False
        if int(vector_meta.get("dimensions", 0)) != EMBEDDING_DIMS:
            return False
        if (
            vector_meta.get("task") != EMBEDDING_TASK_DOCUMENT
            and vector_meta.get("task_type_document") != EMBEDDING_TASK_DOCUMENT
        ):
            # allow both keys
            if vector_meta.get("task") not in (None, EMBEDDING_TASK_DOCUMENT):
                return False
        if vector_meta.get("distance_measure") not in (EMBEDDING_DISTANCE, None):
            # distance may be stored as distance_measure
            if vector_meta.get("distance_measure") != EMBEDDING_DISTANCE:
                return False
        schema = (
            vector_meta.get("schema_version")
            or vector_meta.get("schemaVersion")
            or vector_meta.get("embedding_version")
        )
        if schema not in (EMBEDDING_SCHEMA_VERSION, None, ""):
            # allow empty for legacy but new vectors must have emb-768-v1
            if schema != EMBEDDING_SCHEMA_VERSION:
                return False
        return True
    except Exception:
        return False


def build_delta_plan(
    prior_entries: dict[str, Any],
    candidate_entries: dict[str, Any],
    excluded_entries: dict[str, dict[str, str]] | None = None,
    prior_vectors_meta: dict[str, dict[str, Any]] | None = None,
    prior_release: str = "v1",
    candidate_release: str = "v2",
    plan_id: str | None = None,
) -> DeltaPlan:
    """Build disjoint/exhaustive delta sets conserving eligible/excluded IDs.

    Classification ignores raw retrieval timestamps and metadata-only changes,
    using canonical_content_hash and embedding-document hash plus compatible
    metadata. Raw hash differences alone do not create changed.

    prior_entries: id -> KBBIEntry or dict
    candidate_entries: id -> KBBIEntry or dict (eligible only)
    excluded_entries: id -> {source_kind, reason}
    prior_vectors_meta: id -> metadata dict for compatibility check
    """
    prior_ids = sorted(prior_entries.keys())
    eligible_ids = sorted(candidate_entries.keys())
    excluded_map = excluded_entries or {}
    excluded_ids = sorted(excluded_map.keys())
    candidate_input_ids = sorted(set(eligible_ids) | set(excluded_ids))

    old_canonical: dict[str, str] = {}
    new_canonical: dict[str, str] = {}
    old_doc: dict[str, str] = {}
    new_doc: dict[str, str] = {}
    old_raw: dict[str, str] = {}
    new_raw: dict[str, str] = {}
    compatible: dict[str, bool] = {}
    reused_from: dict[str, str] = {}
    origin_release: dict[str, str] = {}

    for eid, entry in prior_entries.items():
        old_canonical[eid] = canonical_hash_for_entry(entry)
        old_doc[eid] = document_hash_for_entry(entry)
        src = getattr(entry, "source", None)
        if src is not None:
            old_raw[eid] = getattr(src, "content_hash", "") or ""
        elif isinstance(entry, dict):
            srcd = entry.get("source", {})
            old_raw[eid] = srcd.get("content_hash") or srcd.get("contentHash") or ""

    for eid, entry in candidate_entries.items():
        new_canonical[eid] = canonical_hash_for_entry(entry)
        new_doc[eid] = document_hash_for_entry(entry)
        src = getattr(entry, "source", None)
        if src is not None:
            new_raw[eid] = getattr(src, "content_hash", "") or ""
        elif isinstance(entry, dict):
            srcd = entry.get("source", {})
            new_raw[eid] = srcd.get("content_hash") or srcd.get("contentHash") or ""

    vectors_meta = prior_vectors_meta or {}
    for eid in set(prior_ids) & set(eligible_ids):
        meta = vectors_meta.get(eid)
        # When no prior vector exists, assume compatible for planning; actual reuse will create fresh if needed
        compatible[eid] = _is_compatible_metadata(meta) if meta is not None else True

    new_ids: list[str] = []
    changed_ids: list[str] = []
    unchanged_ids: list[str] = []
    removed_ids: list[str] = []

    prior_set = set(prior_ids)
    eligible_set = set(eligible_ids)

    for eid in eligible_set - prior_set:
        new_ids.append(eid)
    for eid in prior_set - eligible_set:
        removed_ids.append(eid)
    for eid in prior_set & eligible_set:
        old_c = old_canonical.get(eid, "")
        new_c = new_canonical.get(eid, "")
        old_d = old_doc.get(eid, "")
        new_d = new_doc.get(eid, "")
        is_compat = compatible.get(eid, True)
        if old_c == new_c and old_d == new_d and is_compat:
            unchanged_ids.append(eid)
            reused_from[eid] = f"{eid}_{prior_release}"
            origin_release[eid] = prior_release
        else:
            changed_ids.append(eid)

    # Validate disjoint/exhaustive invariants
    all_four = set(new_ids) | set(changed_ids) | set(unchanged_ids) | set(removed_ids)
    expected_union = prior_set | eligible_set
    assert all_four == expected_union, f"union mismatch {all_four} vs {expected_union}"
    assert len(set(new_ids) & set(changed_ids)) == 0
    assert len(set(new_ids) & set(unchanged_ids)) == 0
    assert len(set(new_ids) & set(removed_ids)) == 0
    assert len(set(changed_ids) & set(unchanged_ids)) == 0
    assert len(set(changed_ids) & set(removed_ids)) == 0
    assert len(set(unchanged_ids) & set(removed_ids)) == 0

    excluded_records = [
        ExcludedRecord(
            entry_id=eid,
            source_kind=excluded_map[eid].get("source_kind", "unknown"),
            reason=excluded_map[eid].get("reason", "excluded"),
        )
        for eid in sorted(excluded_map.keys())
    ]

    pid = plan_id or f"plan-{prior_release}-to-{candidate_release}"
    return DeltaPlan(
        plan_id=pid,
        prior_release=prior_release,
        candidate_release=candidate_release,
        prior_ids=prior_ids,
        candidate_input_ids=candidate_input_ids,
        eligible_candidate_ids=eligible_ids,
        excluded_ids=excluded_records,
        new_ids=sorted(new_ids),
        changed_ids=sorted(changed_ids),
        unchanged_ids=sorted(unchanged_ids),
        removed_ids=sorted(removed_ids),
        old_canonical_hash=old_canonical,
        new_canonical_hash=new_canonical,
        old_document_hash=old_doc,
        new_document_hash=new_doc,
        compatible_metadata=compatible,
        reused_from=reused_from,
        origin_release=origin_release,
        old_raw_hash=old_raw,
        new_raw_hash=new_raw,
    )
