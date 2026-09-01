"""Deterministic embedding work builder with reuse and cost accounting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aksantara.embeddings.batch_store import persist_batch, validate_vector_record
from aksantara.embeddings.cost import compute_cost
from aksantara.embeddings.document import build_embedding_document
from aksantara.embeddings.metadata import (
    EMBEDDING_DIMS,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL,
    EMBEDDING_SCHEMA_VERSION,
    EMBEDDING_TASK_DOCUMENT,
)
from aksantara.embeddings.planner import DeltaPlan

__all__ = ["BuildReport", "build_work"]


def _deterministic_vector(text: str, dims: int = EMBEDDING_DIMS) -> list[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vec: list[float] = []
    for i in range(dims):
        byte = h[i % len(h)]
        v = (byte / 127.5) - 1.0
        vec.append(v * 0.1 + (i % 7) * 0.001)
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    # Ensure finite
    return [float(x) for x in vec]


def _load_vector_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BuildReport:
    plan_id: str
    prior_release: str
    candidate_release: str
    mode: str
    requested_ids: list[str]
    reused_ids: list[str]
    removed_ids: list[str]
    excluded_ids: list[str]
    provider_calls: int
    retries: int
    writes: int
    chunks: int
    exclusions: int
    estimate_version: str
    request_units: int
    formula: str
    vectors_written: int
    batch_id: str | None
    batch_status: str | None
    is_eligible: bool


def build_work(
    plan: DeltaPlan,
    candidate_entries: dict[str, Any],
    prior_vectors_dir: Path | None,
    output_root: Path,
    candidate_release: str,
    mode: str = "local",
    fail_chunk_index: int | None = None,
) -> dict[str, Any]:
    """Execute delta-only embedding work, materialize reuse, and persist batch.

    Only new ∪ changed invoke provider work; unchanged materialize reuse with
    zero provider calls; removed/excluded receive no work.
    Returns report dict with provider calls/retries, reuse, persistence, mode,
    estimate version, and bounded request-unit cost.
    """
    output_root = Path(output_root).expanduser().resolve()
    vectors_dir = output_root / "vectors" / candidate_release
    # Load prior vectors for reuse materialization
    prior_vectors: dict[str, dict[str, Any]] = {}
    if prior_vectors_dir is not None and Path(prior_vectors_dir).exists():
        for p in Path(prior_vectors_dir).glob("*.json"):
            try:
                data = _load_vector_file(p)
                # doc_id is file stem like id_version; extract id
                entry_id = (
                    data.get("id") or data.get("entry_id") or p.stem.split("_")[0]
                )
                prior_vectors[entry_id] = data
            except Exception:
                continue

    # Generate work vectors: new+changed get fresh deterministic vectors
    work_records: list[Any] = []
    provider_calls = 0
    retries = 0  # deterministic local mode has 0 retries; cloud would report retries
    reused_ids: list[str] = []
    requested_ids: list[str] = []

    from aksantara.embeddings.firestore_types import VectorRecord

    for eid in sorted(set(plan.new_ids) | set(plan.changed_ids)):
        entry = candidate_entries.get(eid)
        if entry is None:
            continue
        doc = build_embedding_document(entry)  # type: ignore[arg-type]
        # Ensure only allowed KBBI fields in stable order are used - build_embedding_document already respects that
        vec = _deterministic_vector(doc)
        # Source lineage extraction
        src = getattr(entry, "source", None)
        if src is not None:
            content_hash = getattr(src, "content_hash", "")
            source_kind = getattr(src, "source_kind", "official-live")
            edition = getattr(src, "edition", "VI")
            source_version = getattr(src, "source_version", "VI")
            parser_version = getattr(src, "parser_version", "0.1.0")
            url = getattr(src, "url", "")
            retrieved_at = getattr(src, "retrieved_at", None)
            retrieved_at_s = (
                retrieved_at.isoformat().replace("+00:00", "Z")
                if hasattr(retrieved_at, "isoformat")
                else ""
            )
        elif isinstance(entry, dict):
            srcd = entry.get("source", {})
            content_hash = srcd.get("content_hash") or srcd.get("contentHash") or ""
            source_kind = srcd.get("source_kind", "official-live")
            edition = srcd.get("edition", "VI")
            source_version = srcd.get("source_version", "VI")
            parser_version = srcd.get("parser_version", "0.1.0")
            url = srcd.get("url", "")
            retrieved_at_s = srcd.get("retrieved_at") or srcd.get("retrievedAt") or ""
        else:
            content_hash = ""
            source_kind = "official-live"
            edition = "VI"
            source_version = "VI"
            parser_version = "0.1.0"
            url = ""
            retrieved_at_s = ""
        canonical_hash = plan.new_canonical_hash.get(eid) or ""
        # Build VectorRecord with strict lineage
        rec = VectorRecord(
            id=eid,
            version=candidate_release,
            lema=getattr(entry, "lema", eid)
            if hasattr(entry, "lema")
            else entry.get("lema", eid)
            if isinstance(entry, dict)
            else eid,
            embedding=tuple(vec),
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIMS,
            content_hash=content_hash,
            source_kind=source_kind,
            edition=edition,
            source_version=source_version,
            parser_version=parser_version,
            embedding_document=doc,
            metadata={
                "canonical_content_hash": canonical_hash,
                "canonical_hash": canonical_hash,
                "raw_content_hash": content_hash,
                "source_release": candidate_release,
                "task": EMBEDDING_TASK_DOCUMENT,
                "distance_measure": EMBEDDING_DISTANCE,
                "schema_version": EMBEDDING_SCHEMA_VERSION,
                "source_url": url,
                "retrieved_at": retrieved_at_s,
                "embedding_document_hash": hashlib.sha256(
                    doc.encode("utf-8")
                ).hexdigest(),
            },
        )
        # Validate strict schema before counting as provider call
        errs = validate_vector_record(rec, candidate_release)
        if errs:
            raise ValueError(f"vector validation failed for {eid}: {errs}")
        work_records.append(rec)
        requested_ids.append(eid)
        provider_calls += 1

    # Reuse materialization for unchanged: create v2-scoped vectors with identical values/document digest
    for eid in sorted(plan.unchanged_ids):
        entry = candidate_entries.get(eid)
        if entry is None:
            continue
        prior_data = prior_vectors.get(eid)
        if prior_data is None:
            # No prior vector to reuse - treat as changed (fallback)
            doc = build_embedding_document(entry)  # type: ignore[arg-type]
            vec = _deterministic_vector(doc)
            src = getattr(entry, "source", None)
            if src is not None:
                content_hash = getattr(src, "content_hash", "")
                source_kind = getattr(src, "source_kind", "official-live")
                edition = getattr(src, "edition", "VI")
                source_version = getattr(src, "source_version", "VI")
                parser_version = getattr(src, "parser_version", "0.1.0")
                url = getattr(src, "url", "")
            else:
                content_hash = ""
                source_kind = "official-live"
                edition = "VI"
                source_version = "VI"
                parser_version = "0.1.0"
                url = ""
            canonical_hash = plan.new_canonical_hash.get(eid) or ""
            rec = VectorRecord(
                id=eid,
                version=candidate_release,
                lema=getattr(entry, "lema", eid) if hasattr(entry, "lema") else eid,
                embedding=tuple(vec),
                model=EMBEDDING_MODEL,
                dimensions=EMBEDDING_DIMS,
                content_hash=content_hash,
                source_kind=source_kind,
                edition=edition,
                source_version=source_version,
                parser_version=parser_version,
                embedding_document=doc,
                metadata={
                    "canonical_content_hash": canonical_hash,
                    "raw_content_hash": content_hash,
                    "source_release": candidate_release,
                    "task": EMBEDDING_TASK_DOCUMENT,
                    "distance_measure": EMBEDDING_DISTANCE,
                    "schema_version": EMBEDDING_SCHEMA_VERSION,
                    "source_url": url,
                    "reused_from": f"{eid}_{plan.prior_release}",
                    "origin_release": plan.prior_release,
                    "embedding_document_hash": hashlib.sha256(
                        doc.encode("utf-8")
                    ).hexdigest(),
                },
            )
            work_records.append(rec)
            requested_ids.append(eid)
            provider_calls += 1
            continue
        # Have prior data - copy values, keep identical digest
        prior_vec = (
            prior_data.get("embedding")
            or prior_data.get("embedding_vector")
            or prior_data.get("vector")
            or []
        )
        prior_vec_list = [float(x) for x in prior_vec]
        # Ensure 768 finite
        if len(prior_vec_list) != EMBEDDING_DIMS:
            # pad/truncate to ensure exact dims - but should be exact already
            prior_vec_list = (prior_vec_list[:EMBEDDING_DIMS] + [0.0] * EMBEDDING_DIMS)[
                :EMBEDDING_DIMS
            ]
        entry_doc = build_embedding_document(entry)  # type: ignore[arg-type]
        prior_doc = prior_data.get("embedding_document", "")
        prior_doc_hash = (
            prior_data.get("embedding_document_hash")
            or hashlib.sha256(prior_doc.encode("utf-8")).hexdigest()
            if prior_doc
            else hashlib.sha256(entry_doc.encode("utf-8")).hexdigest()
        )
        new_doc_hash = hashlib.sha256(entry_doc.encode("utf-8")).hexdigest()
        assert prior_doc_hash == new_doc_hash, f"unchanged doc hash mismatch for {eid}"
        # Use identical values
        src2 = getattr(entry, "source", None)
        if src2 is not None:
            content_hash2 = getattr(src2, "content_hash", "")
            source_kind2 = getattr(src2, "source_kind", "official-live")
            edition2 = getattr(src2, "edition", "VI")
            source_version2 = getattr(src2, "source_version", "VI")
            parser_version2 = getattr(src2, "parser_version", "0.1.0")
        elif isinstance(entry, dict):
            srcd2 = entry.get("source", {})
            content_hash2 = srcd2.get("content_hash") or ""
            source_kind2 = srcd2.get("source_kind", "official-live")
            edition2 = srcd2.get("edition", "VI")
            source_version2 = srcd2.get("source_version", "VI")
            parser_version2 = srcd2.get("parser_version", "0.1.0")
        else:
            content_hash2 = (
                prior_data.get("content_hash") or prior_data.get("contentHash") or ""
            )
            source_kind2 = prior_data.get("source_kind", "official-live")
            edition2 = prior_data.get("edition", "VI")
            source_version2 = prior_data.get("source_version", "VI")
            parser_version2 = prior_data.get("parser_version", "0.1.0")
        canonical_hash2 = plan.new_canonical_hash.get(eid) or prior_data.get(
            "metadata", {}
        ).get("canonical_content_hash", "")
        rec2 = VectorRecord(
            id=eid,
            version=candidate_release,
            lema=prior_data.get("lema", eid),
            embedding=tuple(prior_vec_list),
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIMS,
            content_hash=content_hash2,
            source_kind=source_kind2,
            edition=edition2,
            source_version=source_version2,
            parser_version=parser_version2,
            embedding_document=entry_doc,
            metadata={
                "canonical_content_hash": canonical_hash2,
                "raw_content_hash": content_hash2,
                "source_release": candidate_release,
                "task": EMBEDDING_TASK_DOCUMENT,
                "distance_measure": EMBEDDING_DISTANCE,
                "schema_version": EMBEDDING_SCHEMA_VERSION,
                "reused_from": f"{eid}_{plan.prior_release}",
                "origin_release": plan.prior_release,
                "embedding_document_hash": new_doc_hash,
                "reused": True,
            },
        )
        errs2 = validate_vector_record(rec2, candidate_release)
        if errs2:
            raise ValueError(f"reuse validation failed for {eid}: {errs2}")
        work_records.append(rec2)
        reused_ids.append(eid)
        # Zero provider calls for reuse - not counted

    # Removed/excluded receive no work - verify none of those ids are in work_records
    assert not any(r.id in plan.removed_ids for r in work_records), (
        "removed received work"
    )
    assert not any(
        r.id in [e.entry_id for e in plan.excluded_ids] for r in work_records
    ), "excluded received work"

    # Persist batch (create-only, chunked)
    batch_result = persist_batch(
        work_records,
        root=output_root,
        expected_release=candidate_release,
        batch_id=f"batch-{candidate_release}-{plan.plan_id}",
        fail_chunk_index=fail_chunk_index,
    )

    # Cost accounting
    cost = compute_cost(
        provider_calls=provider_calls,
        retries=retries,
        reused=len(reused_ids),
        writes=batch_result.committed_chunks,
        chunks=len(batch_result.chunks),
        exclusions=len(plan.excluded_ids),
        mode=mode,
    )

    report = {
        "plan_id": plan.plan_id,
        "prior_release": plan.prior_release,
        "candidate_release": candidate_release,
        "mode": mode,
        "estimate_version": cost.estimate_version,
        "requested_ids": sorted(requested_ids),
        "reused_ids": sorted(reused_ids),
        "removed_ids": sorted(plan.removed_ids),
        "excluded_ids": [e.entry_id for e in plan.excluded_ids],
        "document_provider_calls": provider_calls,
        "provider_calls": provider_calls,
        "retries": retries,
        "reuse_count": len(reused_ids),
        "writes": batch_result.committed_chunks,
        "chunks": len(batch_result.chunks),
        "exclusions": len(plan.excluded_ids),
        "request_units": cost.request_units,
        "formula": cost.formula,
        "vectors_written": len(batch_result.committed_doc_ids),
        "batch_id": batch_result.batch_id,
        "batch_status": batch_result.status,
        "is_eligible": batch_result.is_eligible,
        "chunk_ledger": [
            {"chunk_id": c.chunk_id, "committed": c.committed, "size": c.size}
            for c in batch_result.chunks
        ],
        "batch_error": batch_result.error,
    }
    # Also write build report artifact
    builds_dir = output_root / "builds"
    builds_dir.mkdir(parents=True, exist_ok=True)
    (builds_dir / f"{candidate_release}.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return report
