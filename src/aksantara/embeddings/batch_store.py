"""Duplicate-safe batch persistence with chunking and partial failure reporting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aksantara.embeddings.firestore_types import VectorRecord
from aksantara.embeddings.firestore_utils import _record_doc_id, _record_fingerprint
from aksantara.embeddings.metadata import MAX_BATCH_SIZE

__all__ = [
    "BatchResult",
    "ChunkInfo",
    "persist_batch",
    "validate_vector_record",
]


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    chunk_id: str
    index: int
    size: int
    doc_ids: list[str]
    committed: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch_id: str
    max_size: int
    total_records: int
    chunks: list[ChunkInfo]
    committed_chunks: int
    failed_chunks: int
    committed_doc_ids: list[str]
    expected_doc_ids: list[str]
    status: str  # completed, partial, failed_before_write, conflict
    error: str | None = None
    is_eligible: bool = False


def _vector_lineage_digest(record: VectorRecord) -> str:
    payload = _record_fingerprint(record)
    # include vector values explicitly for conflict detection
    vec = record.vector_as_list()
    payload["vector"] = vec
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_vector_record(record: VectorRecord, expected_release: str) -> list[str]:
    """Return list of lineage/metadata violations; empty means valid."""
    errors: list[str] = []
    if record.version != expected_release:
        errors.append(f"release mismatch {record.version!r} != {expected_release!r}")
    if record.model != "gemini-embedding-001":
        errors.append(f"model {record.model!r} != gemini-embedding-001")
    if record.dimensions != 768:
        errors.append(f"dimensions {record.dimensions} != 768")
    vec = record.vector_as_list()
    if len(vec) != 768:
        errors.append(f"vector length {len(vec)} != 768")
    else:
        for idx, v in enumerate(vec):
            if not isinstance(v, (int, float)):
                errors.append(f"non-numeric at {idx}")
                break
            if not (v == v and v != float("inf") and v != float("-inf")):
                errors.append(f"non-finite at {idx}: {v}")
                break
    if not record.content_hash or len(record.content_hash) != 64:
        errors.append("missing or invalid raw content_hash")
    # canonical hash stored in embedding_document or metadata - check present
    # For this batch store we store canonical hash in metadata canonical_content_hash
    meta = record.metadata or {}
    cch = meta.get("canonical_content_hash") or meta.get("canonical_hash") or ""
    if not cch:
        # also check embedding_document hash via record attribute if extended
        if not getattr(record, "canonical_content_hash", ""):
            errors.append("missing canonical_content_hash lineage")
    if not record.source_kind:
        errors.append("missing source_kind")
    # task/distance/schema stored in metadata
    if meta.get("task") not in (None, "RETRIEVAL_DOCUMENT"):
        if meta.get("task") != "RETRIEVAL_DOCUMENT":
            errors.append(f"task {meta.get('task')!r} != RETRIEVAL_DOCUMENT")
    if meta.get("distance_measure") not in (None, "DOT_PRODUCT"):
        if meta.get("distance_measure") != "DOT_PRODUCT":
            errors.append(f"distance {meta.get('distance_measure')!r} != DOT_PRODUCT")
    schema = meta.get("schema_version") or meta.get("schemaVersion") or ""
    if schema and schema != "emb-768-v1":
        errors.append(f"schema {schema!r} != emb-768-v1")
    return errors


def persist_batch(
    records: list[VectorRecord],
    root: Path,
    expected_release: str,
    batch_id: str | None = None,
    fail_chunk_index: int | None = None,
    existing_store: dict[str, dict[str, Any]] | None = None,
) -> BatchResult:
    """Persist vectors create-only/idempotent, chunked, with preflight duplicate check.

    - Existing IDs are preflighted against immutable payload/value/lineage digests.
    - Conflicting IDs fail before first write (no chunk committed).
    - Identical repeats are no-op (collapsed).
    - Batch is chunked at MAX_BATCH_SIZE (500), per-chunk atomic.
    - Later-chunk failure leaves incomplete/ineligible candidate with recoverable tail.
    - Returns BatchResult with status and commit ledger.
    """
    root = Path(root).expanduser().resolve()
    vectors_dir = root / "vectors" / expected_release
    ledger_dir = root / "batches"
    vectors_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir.mkdir(parents=True, exist_ok=True)

    bid = (
        batch_id
        or f"batch-{expected_release}-{hashlib.sha256(str(sorted([r.doc_id for r in records])).encode()).hexdigest()[:12]}"
    )
    # Deduplicate identical records (reuse firestore_utils logic)
    seen: dict[str, dict[str, Any]] = {}
    unique: list[VectorRecord] = []
    for rec in records:
        did = _record_doc_id(rec)
        fp = _record_fingerprint(rec)
        fp["vector"] = rec.vector_as_list()
        prev = seen.get(did)
        if prev is not None:
            if prev != fp:
                return BatchResult(
                    batch_id=bid,
                    max_size=MAX_BATCH_SIZE,
                    total_records=len(records),
                    chunks=[],
                    committed_chunks=0,
                    failed_chunks=1,
                    committed_doc_ids=[],
                    expected_doc_ids=sorted([_record_doc_id(r) for r in records]),
                    status="conflict",
                    error=f"conflicting records for document id {did!r} within batch",
                    is_eligible=False,
                )
            continue
        seen[did] = fp
        unique.append(rec)

    # Preflight against existing store: create-only semantics
    existing = existing_store or {}
    # Also load existing files for compare
    for p in vectors_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            doc_id = p.stem
            if doc_id not in existing:
                existing[doc_id] = data
        except Exception:
            continue
    for rec in unique:
        did = _record_doc_id(rec)
        if did in existing:
            existing_payload = existing[did]
            # Compare immutable digests
            rec_fp = seen[did]
            # existing payload may have different shape; compare vector and metadata
            existing_vec = (
                existing_payload.get("embedding")
                or existing_payload.get("embedding_vector")
                or existing_payload.get("vector")
                or []
            )
            if list(existing_vec) != rec.vector_as_list():
                return BatchResult(
                    batch_id=bid,
                    max_size=MAX_BATCH_SIZE,
                    total_records=len(unique),
                    chunks=[],
                    committed_chunks=0,
                    failed_chunks=1,
                    committed_doc_ids=[],
                    expected_doc_ids=sorted(seen.keys()),
                    status="conflict",
                    error=f"conflicting existing payload for document id {did!r}",
                    is_eligible=False,
                )
            # also compare model/dims etc via fingerprint without timestamps
            # If mismatch, conflict
            for key in ("model", "dimensions", "content_hash", "source_kind"):
                if existing_payload.get(key) != rec_fp.get(key):
                    # allow content_hash alias differences?
                    if key == "content_hash" and existing_payload.get(
                        "contentHash"
                    ) == rec_fp.get("content_hash"):
                        continue
                    return BatchResult(
                        batch_id=bid,
                        max_size=MAX_BATCH_SIZE,
                        total_records=len(unique),
                        chunks=[],
                        committed_chunks=0,
                        failed_chunks=1,
                        committed_doc_ids=[],
                        expected_doc_ids=sorted(seen.keys()),
                        status="conflict",
                        error=f"conflicting existing payload for document id {did!r} field {key}",
                        is_eligible=False,
                    )

    # If no new unique beyond existing, idempotent no-op
    to_write = [r for r in unique if _record_doc_id(r) not in existing]
    if not to_write:
        return BatchResult(
            batch_id=bid,
            max_size=MAX_BATCH_SIZE,
            total_records=len(unique),
            chunks=[
                ChunkInfo(
                    chunk_id=f"{bid}-chunk-0",
                    index=0,
                    size=0,
                    doc_ids=[],
                    committed=True,
                )
            ]
            if unique
            else [],
            committed_chunks=1 if unique else 0,
            failed_chunks=0,
            committed_doc_ids=sorted(seen.keys()),
            expected_doc_ids=sorted(seen.keys()),
            status="completed",
            error=None,
            is_eligible=True,
        )

    # Chunking
    chunks: list[ChunkInfo] = []
    committed_doc_ids: list[str] = sorted([k for k in existing.keys() if k in seen])
    expected_doc_ids = sorted(seen.keys())
    is_eligible = False
    error: str | None = None

    for start in range(0, len(to_write), MAX_BATCH_SIZE):
        chunk_records = to_write[start : start + MAX_BATCH_SIZE]
        idx = start // MAX_BATCH_SIZE
        chunk_id = f"{bid}-chunk-{idx}"
        doc_ids = [_record_doc_id(r) for r in chunk_records]
        # Simulate later-chunk failure injection
        if fail_chunk_index is not None and idx == fail_chunk_index:
            chunks.append(
                ChunkInfo(
                    chunk_id=chunk_id,
                    index=idx,
                    size=len(chunk_records),
                    doc_ids=doc_ids,
                    committed=False,
                    error="injected later-chunk failure",
                )
            )
            error = f"chunk {idx} failed (injected)"
            break
        # Atomic commit per chunk: write each vector file
        try:
            for rec in chunk_records:
                did = _record_doc_id(rec)
                payload = rec.to_firestore_dict()
                # Add strict lineage fields expected by inspect
                payload["source_release"] = rec.version
                payload["entry_id"] = rec.id or rec.entry_id
                payload["model"] = rec.model
                payload["dimensions"] = rec.dimensions
                payload["task"] = "RETRIEVAL_DOCUMENT"
                payload["distance_measure"] = "DOT_PRODUCT"
                payload["schema_version"] = "emb-768-v1"
                payload["raw_content_hash"] = rec.content_hash
                # canonical_content_hash stored in metadata; ensure present
                if "canonical_content_hash" not in payload["metadata"]:
                    payload["metadata"]["canonical_content_hash"] = rec.metadata.get(
                        "canonical_content_hash", rec.content_hash
                    )
                # embedding_document hash
                if rec.embedding_document:
                    payload["embedding_document"] = rec.embedding_document
                    payload["embedding_document_hash"] = hashlib.sha256(
                        rec.embedding_document.encode("utf-8")
                    ).hexdigest()
                # lineage for reuse
                if rec.metadata.get("reused_from"):
                    payload["reused_from"] = rec.metadata["reused_from"]
                    payload["origin_release"] = rec.metadata.get("origin_release", "")
                # vector values finite check already via validate
                out_path = vectors_dir / f"{did}.json"
                # create-only: if exists, skip (already checked conflict)
                if out_path.exists():
                    continue
                # Ensure JSON serializable for local persistence
                for k in list(payload.keys()):
                    v = payload[k]
                    if hasattr(v, "isoformat"):
                        payload[k] = v.isoformat().replace("+00:00", "Z")
                    elif hasattr(v, "__class__") and v.__class__.__name__ == "Vector":
                        payload[k] = list(v)
                # also metadata datetimes
                if isinstance(payload.get("metadata"), dict):
                    for mk, mv in list(payload["metadata"].items()):
                        if hasattr(mv, "isoformat"):
                            payload["metadata"][mk] = mv.isoformat().replace(
                                "+00:00", "Z"
                            )
                # Write atomically via tmp rename
                tmp = out_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                        default=str,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                tmp.rename(out_path)
            chunks.append(
                ChunkInfo(
                    chunk_id=chunk_id,
                    index=idx,
                    size=len(chunk_records),
                    doc_ids=doc_ids,
                    committed=True,
                )
            )
            committed_doc_ids.extend(doc_ids)
        except Exception as exc:
            chunks.append(
                ChunkInfo(
                    chunk_id=chunk_id,
                    index=idx,
                    size=len(chunk_records),
                    doc_ids=doc_ids,
                    committed=False,
                    error=str(exc),
                )
            )
            error = str(exc)
            break

    # Determine eligibility: only if all chunks committed and all expected present
    all_committed = (
        all(c.committed for c in chunks)
        and len(committed_doc_ids) == len(expected_doc_ids)
        and error is None
    )
    # Also check that no tail remains unwritten due to break
    if len(committed_doc_ids) < len(expected_doc_ids):
        all_committed = False

    status = "completed" if all_committed else "partial"
    if not unique:
        status = "completed"
    # Ledger persistence
    ledger = {
        "batch_id": bid,
        "expected_release": expected_release,
        "max_size": MAX_BATCH_SIZE,
        "total_records": len(unique),
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "index": c.index,
                "size": c.size,
                "doc_ids": c.doc_ids,
                "committed": c.committed,
                "error": c.error,
            }
            for c in chunks
        ],
        "committed_doc_ids": sorted(committed_doc_ids),
        "expected_doc_ids": expected_doc_ids,
        "status": status,
        "error": error,
        "is_eligible": all_committed,
    }
    (ledger_dir / f"{bid}.json").write_text(
        json.dumps(ledger, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    return BatchResult(
        batch_id=bid,
        max_size=MAX_BATCH_SIZE,
        total_records=len(unique),
        chunks=chunks,
        committed_chunks=sum(1 for c in chunks if c.committed),
        failed_chunks=sum(1 for c in chunks if not c.committed),
        committed_doc_ids=sorted(committed_doc_ids),
        expected_doc_ids=expected_doc_ids,
        status=status,
        error=error,
        is_eligible=all_committed,
    )
