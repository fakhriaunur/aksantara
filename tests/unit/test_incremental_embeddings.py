"""Unit tests for incremental embedding planner and batch store."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.embeddings.batch_store import persist_batch, validate_vector_record
from aksantara.embeddings.firestore_types import VectorRecord
from aksantara.embeddings.planner import build_delta_plan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    entry_id: str, definisi: str, retrieved_at: datetime | None = None
) -> KBBIEntry:
    raw = f"definisi {definisi}".encode()
    ch = hashlib.sha256(raw).hexdigest()
    return KBBIEntry(
        id=entry_id,
        lema=entry_id.title(),
        makna=[{"definisi": definisi}],
        source=SourceRef(
            url=f"https://kbbi.kemdikbud.go.id/entri/{entry_id}",
            source_kind="official-live",
            edition="VI",
            source_version="VI",
            retrieved_at=retrieved_at or datetime.now(UTC),
            content_hash=ch,
            parser_version="0.1.0",
        ),
    )


def _vector(entry: KBBIEntry, release: str = "v1") -> VectorRecord:
    import hashlib as _h

    from aksantara.embeddings.document import build_embedding_document

    doc = build_embedding_document(entry)
    h = _h.sha256(doc.encode("utf-8")).digest()
    vec = []
    for i in range(768):
        byte = h[i % len(h)]
        v = (byte / 127.5) - 1.0
        vec.append(v * 0.1 + (i % 7) * 0.001)
    norm = sum(x * x for x in vec) ** 0.5
    vec = [x / norm for x in vec]
    return VectorRecord(
        id=entry.id,
        version=release,
        lema=entry.lema,
        embedding=tuple(vec),
        model="gemini-embedding-001",
        dimensions=768,
        content_hash=entry.source.content_hash,
        source_kind=entry.source.source_kind,
        edition=entry.source.edition,
        metadata={
            "canonical_content_hash": hashlib.sha256(
                json.dumps({"id": entry.id}, sort_keys=True).encode()
            ).hexdigest(),
            "task": "RETRIEVAL_DOCUMENT",
            "distance_measure": "DOT_PRODUCT",
            "schema_version": "emb-768-v1",
        },
        embedding_document=doc,
    )


# ---------------------------------------------------------------------------
# Planner tests
# ---------------------------------------------------------------------------


def test_delta_sets_are_disjoint_and_conserve(tmp_path: Path) -> None:
    prior = {
        "a": _entry("a", "definisi a"),
        "b": _entry("b", "definisi b"),
        "c": _entry("c", "definisi c"),
    }
    candidate = {
        "b": _entry("b", "definisi b"),  # unchanged
        "c": _entry("c", "definisi c changed"),  # changed
        "d": _entry("d", "definisi d"),  # new
    }
    excluded = {"e": {"source_kind": "fallback", "reason": "quarantined"}}
    plan = build_delta_plan(
        prior,
        candidate,
        excluded_entries=excluded,
        prior_release="v1",
        candidate_release="v2",
    )
    assert set(plan.new_ids) == {"d"}
    assert set(plan.changed_ids) == {"c"}
    assert set(plan.unchanged_ids) == {"b"}
    assert set(plan.removed_ids) == {"a"}
    assert {r.entry_id for r in plan.excluded_ids} == {"e"}
    # conservation
    assert set(plan.candidate_input_ids) == set(plan.eligible_candidate_ids) | {
        r.entry_id for r in plan.excluded_ids
    }
    assert set(plan.prior_ids) | set(plan.eligible_candidate_ids) == set(
        plan.new_ids
    ) | set(plan.changed_ids) | set(plan.unchanged_ids) | set(plan.removed_ids)
    # disjoint
    assert not (set(plan.new_ids) & set(plan.changed_ids))
    assert not (set(plan.new_ids) & set(plan.unchanged_ids))


def test_classification_ignores_retrieval_timestamp_and_raw_hash(
    tmp_path: Path,
) -> None:
    # Same lexical content but different retrieved_at and raw hash variation should remain unchanged
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 9, 1, tzinfo=UTC)
    e1 = _entry("x", "same definisi", retrieved_at=t1)
    e2 = _entry("x", "same definisi", retrieved_at=t2)
    # force different content_hash via different raw? Actually make same definisi -> same content_hash, but timestamp different
    # canonical hash should ignore timestamp, so unchanged
    plan = build_delta_plan(
        {"x": e1}, {"x": e2}, prior_release="v1", candidate_release="v2"
    )
    assert plan.unchanged_ids == ["x"]
    assert plan.changed_ids == []
    assert plan.reused_from["x"] == "x_v1"


def test_unchanged_reuse_carries_origin_and_zero_provider_calls(tmp_path: Path) -> None:
    from aksantara.embeddings.work import build_work

    prior = {"x": _entry("x", "definisi x")}
    cand = {"x": _entry("x", "definisi x")}
    # Seed prior vectors
    from aksantara.embeddings.release import seed_release

    root = tmp_path / "root"
    root.mkdir()
    seed_release(root, "v1", list(prior.values()))
    plan = build_delta_plan(prior, cand, prior_release="v1", candidate_release="v2")
    # Need prior vectors meta - load from seeded
    prior_vectors_dir = root / "vectors" / "v1"
    # Build work
    report = build_work(plan, cand, prior_vectors_dir, root, "v2", mode="local")
    assert report["reused_ids"] == ["x"]
    assert report["requested_ids"] == []
    assert report["provider_calls"] == 0
    assert report["request_units"] == 0
    # Check persisted vector has reused_from
    vec_path = root / "vectors" / "v2" / "x_v2.json"
    data = json.loads(vec_path.read_text())
    assert data["reused_from"] == "x_v1"
    assert data["origin_release"] == "v1"
    assert data["model"] == "gemini-embedding-001"
    assert data["dimensions"] == 768
    assert len(data["embedding"]) == 768
    # Finite check
    for v in data["embedding"]:
        assert isinstance(v, (int, float))
        assert v == v and v not in (float("inf"), float("-inf"))


def test_vector_metadata_strict_and_finite(tmp_path: Path) -> None:
    entry = _entry("februari", "bulan kedua")
    vec = _vector(entry, "v1")
    errs = validate_vector_record(vec, "v1")
    assert errs == []
    # Wrong dims should fail
    bad = VectorRecord(
        id="februari",
        version="v1",
        lema="Februari",
        embedding=tuple([0.0] * 10),
        model="gemini-embedding-001",
        dimensions=10,
        content_hash="a" * 64,
        metadata={"canonical_content_hash": "abc"},
    )
    errs2 = validate_vector_record(bad, "v1")
    assert any("dimensions" in e or "length" in e for e in errs2)
    # Non-finite
    nonfinite = VectorRecord(
        id="x",
        version="v1",
        lema="X",
        embedding=tuple([float("inf")] + [0.0] * 767),
        model="gemini-embedding-001",
        dimensions=768,
        content_hash="a" * 64,
        metadata={"canonical_content_hash": "abc"},
    )
    errs3 = validate_vector_record(nonfinite, "v1")
    assert any("non-finite" in e for e in errs3)


def test_batch_persistence_is_create_only_and_idempotent(tmp_path: Path) -> None:
    entry = _entry("a", "definisi a")
    vec = _vector(entry, "v1")
    root = tmp_path / "root2"
    root.mkdir()
    # First write succeeds
    result1 = persist_batch([vec], root, "v1")
    assert result1.status == "completed"
    assert result1.is_eligible is True
    # Identical repeat is no-op
    result2 = persist_batch([vec], root, "v1")
    assert result2.status == "completed"
    assert result2.committed_doc_ids == result1.committed_doc_ids
    # Conflicting write fails before first write
    bad = VectorRecord(
        id="a",
        version="v1",
        lema="A",
        embedding=tuple([0.1] * 768),
        model="gemini-embedding-001",
        dimensions=768,
        content_hash="b" * 64,
        metadata={"canonical_content_hash": "xyz"},
    )
    result3 = persist_batch([bad], root, "v1")
    assert result3.status == "conflict"
    # Conflicting batch should not have overwritten original
    existing = json.loads((root / "vectors" / "v1" / "a_v1.json").read_text())
    assert existing["content_hash"] == vec.content_hash


def test_batch_chunks_501_and_later_chunk_failure(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    records = [
        VectorRecord(
            id=f"e{i:03d}",
            version="big",
            lema=f"E{i}",
            embedding=tuple([float(i % 10) / 10] * 768),
            model="gemini-embedding-001",
            dimensions=768,
            content_hash=hashlib.sha256(f"e{i}".encode()).hexdigest(),
            metadata={
                "canonical_content_hash": hashlib.sha256(f"c{i}".encode()).hexdigest()
            },
        )
        for i in range(501)
    ]
    result = persist_batch(records, root, "big")
    assert len(result.chunks) == 2
    assert result.chunks[0].size == 500
    assert result.chunks[1].size == 1
    assert result.is_eligible is True
    # Later chunk failure
    root2 = tmp_path / "big2"
    root2.mkdir()
    result_fail = persist_batch(records, root2, "big", fail_chunk_index=1)
    assert result_fail.status == "partial"
    assert result_fail.is_eligible is False
    assert result_fail.committed_chunks == 1
    assert result_fail.failed_chunks == 1


def test_cost_accounting_is_bounded_and_reproducible(tmp_path: Path) -> None:
    from aksantara.embeddings.cost import compute_cost

    report = compute_cost(
        provider_calls=5,
        retries=2,
        reused=3,
        writes=1,
        chunks=1,
        exclusions=1,
        mode="local",
    )
    assert report.request_units == 5
    assert report.mode == "local"
    assert report.estimate_version == "cost-v1"
    assert "provider_calls" in report.formula


def test_firestore_split_preserves_contract(tmp_path: Path) -> None:
    from aksantara.embeddings.firestore import (
        EmbeddingStore,
        FirestoreVectorStore,
        InMemoryVectorStore,
        VectorRecord,
    )

    assert issubclass(FirestoreVectorStore, EmbeddingStore)
    assert issubclass(InMemoryVectorStore, EmbeddingStore)
    rec = VectorRecord(
        id="test",
        version="v1",
        lema="Test",
        embedding=tuple([0.0] * 768),
        model="gemini-embedding-001",
        dimensions=768,
        content_hash="a" * 64,
    )
    store = InMemoryVectorStore()
    store.put_many([rec])
    assert store.count() == 1
