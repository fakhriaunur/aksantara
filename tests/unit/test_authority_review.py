from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.domain.provenance import content_hash_bytes
from aksantara.ingest.checkpoint import CheckpointDriver
from aksantara.ingest.snapshots import RawSnapshotStore
from aksantara.validate.conflicts import (
    LEXICAL_FIELDS,
    compare_lexical_fields,
)
from aksantara.validate.review import ReviewStore


def _source(
    raw: bytes,
    *,
    kind: str,
    host: str,
    retrieved_at: str = "2026-08-31T00:00:00Z",
) -> dict[str, object]:
    return {
        "url": f"https://{host}/entri/kata",
        "source_kind": kind,
        "edition": "VI",
        "source_version": "fixture-v1",
        "retrieved_at": datetime.fromisoformat(retrieved_at.replace("Z", "+00:00")),
        "content_hash": content_hash_bytes(raw),
        "parser_version": "0.1.0",
    }


def _raw(definition: str = "makna resmi") -> bytes:
    return (
        "<article><h1>Kata</h1><p class='kelas'>n</p>"
        f"<p class='makna'>{definition}</p>"
        "<p class='contoh'>Contoh kata.</p></article>"
    ).encode()


def _catalog(
    official: bytes | None,
    fallback: bytes | None = None,
    *,
    fallback_kind: str = "fallback",
) -> dict[str, object]:
    entries: dict[str, object] = {
        "stable_key": "kata",
        "source_ref": _source(
            official if official is not None else fallback or b"",
            kind="official-snapshot" if official is not None else fallback_kind,
            host="kbbi.kemdikbud.go.id" if official is not None else "kbbi.web.id",
        ),
        "transport": {
            "adapter": "fixture",
            "content": (official if official is not None else fallback or b"").decode(),
            "content_type": "text/html",
            "expected_raw_hash": content_hash_bytes(
                official if official is not None else fallback or b""
            ),
            "comparison_mode": "exact",
            "status": 200,
        },
    }
    if fallback is not None and official is not None:
        entries["observations"] = [
            {
                "role": "fallback",
                "source_ref": _source(
                    fallback,
                    kind=fallback_kind,
                    host="kbbi.web.id",
                ),
                "transport": {
                    "adapter": "fixture",
                    "content": fallback.decode(),
                    "content_type": "text/html",
                    "expected_raw_hash": content_hash_bytes(fallback),
                    "comparison_mode": "exact",
                    "status": 200,
                },
            }
        ]
    return {
        "catalog_id": "authority-fixture-v1",
        "corpus_version": "kbbi-vi-authority-fixture-v1",
        "authority_mode": "official-first",
        "entries": [entries],
    }


def test_lexical_field_policy_covers_all_documented_fields() -> None:
    assert tuple(LEXICAL_FIELDS) == (
        "lema",
        "sub_lema",
        "ejaan",
        "kelas_kata",
        "makna",
        "contoh",
        "turunan",
        "bentuk_baku",
        "bentuk_tidak_baku",
        "pelafalan",
        "pemenggalan",
        "etimologi",
        "labels",
        "status",
    )


def test_metadata_only_source_difference_is_not_a_lexical_conflict() -> None:
    raw = _raw()
    source_official = SourceRef(
        **_source(
            raw,
            kind="official-snapshot",
            host="kbbi.kemdikbud.go.id",
        )
    )
    source_fallback = SourceRef(
        **_source(
            raw,
            kind="fallback",
            host="kbbi.web.id",
            retrieved_at="2026-08-31T01:00:00Z",
        )
    )
    official = KBBIEntry(
        id="kata",
        lema="Kata",
        kelas_kata=["n"],
        makna=[{"definisi": "makna resmi"}],
        contoh=["Contoh kata."],
        source=source_official,
    )
    fallback = official.model_copy(update={"source": source_fallback})
    assert compare_lexical_fields(official, fallback) == []


def test_driver_quarantines_conflict_and_persists_both_sides(tmp_path: Path) -> None:
    result = CheckpointDriver(root=tmp_path).run(
        _catalog(_raw(), _raw("makna cermin")),
        limit=1,
        idempotency_key="authority-conflict",
    )

    outcome = result.report["outcomes"][0]
    assert result.status == "completed"
    assert outcome["outcome"] == "quarantined"
    conflict_id = outcome["conflict_id"]
    assert outcome["candidate_namespace"] is False
    assert outcome["exclusion_reason"] == "lexical_conflict"
    assert outcome["observations"][0]["source_kind"] == "official-snapshot"
    assert outcome["observations"][1]["source_kind"] == "fallback"

    store = ReviewStore(root=tmp_path)
    conflict = store.get(conflict_id)
    assert conflict["conflict_id"] == conflict_id
    assert conflict["release_blocking"] is True
    assert conflict["review_status"] == "pending"
    assert conflict["differing_fields"] == ["makna"]
    assert conflict["field_diffs"][0]["official_value_hash"]
    assert conflict["field_diffs"][0]["fallback_value_hash"]
    assert conflict["official"]["raw_sha256"] != conflict["fallback"]["raw_sha256"]
    assert store.list_open()[0]["conflict_id"] == conflict_id


def test_review_decision_is_append_only_and_idempotent(tmp_path: Path) -> None:
    result = CheckpointDriver(root=tmp_path).run(
        _catalog(_raw(), _raw("makna cermin")),
        limit=1,
        idempotency_key="review-decision",
    )
    store = ReviewStore(root=tmp_path)
    conflict_id = result.report["outcomes"][0]["conflict_id"]

    first = store.decide(
        conflict_id,
        decision="select_official",
        reviewer="operator-1",
        reason="Official snapshot is authoritative.",
        policy_version="official-first-v1",
        idempotency_key="decision-1",
        timestamp=datetime(2026, 8, 31, tzinfo=UTC),
    )
    repeated = store.decide(
        conflict_id,
        decision="select_official",
        reviewer="operator-1",
        reason="Official snapshot is authoritative.",
        policy_version="official-first-v1",
        idempotency_key="decision-1",
        timestamp=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    assert first["event_id"] == repeated["event_id"]
    current = store.get(conflict_id)
    assert current["review_status"] == "approved"
    assert current["selected_authority"] == "official"
    assert current["release_blocking"] is True
    assert len(current["review_history"]) == 1

    blocked = store.decide(
        conflict_id,
        decision="block",
        reviewer="operator-2",
        reason="Needs editorial review.",
        policy_version="official-first-v1",
        idempotency_key="decision-2",
    )
    assert blocked["decision"] == "block"
    after = store.get(conflict_id)
    assert len(after["review_history"]) == 2
    assert after["review_status"] == "rejected"
    assert after["release_blocking"] is True


def test_fallback_only_never_enters_candidate(tmp_path: Path) -> None:
    result = CheckpointDriver(root=tmp_path).run(
        _catalog(None, _raw()),
        limit=1,
        idempotency_key="fallback-only",
    )
    outcome = result.report["outcomes"][0]
    assert outcome["outcome"] == "quarantined"
    assert outcome["candidate_namespace"] is False
    candidate = CheckpointDriver(root=tmp_path).evaluate_candidate(
        result.run_id,
        release_approved=True,
    )
    assert candidate["eligible"] is False
    assert candidate["candidate_created"] is False
    assert candidate["excluded"][0]["reason"] == "official_required"


def test_official_failure_reads_fallback_only_as_labeled_evidence(
    tmp_path: Path,
) -> None:
    catalog = _catalog(_raw(), _raw("fallback meaning"))
    entry = dict(catalog["entries"][0])  # type: ignore[index]
    transport = dict(entry["transport"])  # type: ignore[index]
    transport["status"] = 404
    entry["transport"] = transport

    driver = CheckpointDriver(root=tmp_path)
    result = driver.run(
        {**catalog, "entries": [entry]},
        limit=1,
        idempotency_key="official-failure",
    )

    outcome = result.report["outcomes"][0]
    assert outcome["outcome"] == "quarantined"
    assert outcome["exclusion_reason"] == "official_required"
    attempts = driver.attempts(result.run_id)["attempts"][0]
    assert [item["source_kind"] for item in attempts["source_attempts"]] == [
        "official-snapshot",
        "fallback",
    ]
    assert attempts["source_attempts"][0]["status"] == 404
    assert attempts["source_attempts"][1]["status"] == 200
    assert outcome["observations"][1]["source_kind"] == "fallback"
    assert ReviewStore(root=tmp_path).list_open()[0]["reason"] == (
        "official_transport_failure"
    )


def test_retryable_primary_selects_successful_backup_official(
    tmp_path: Path,
) -> None:
    primary = _raw("primary")
    backup = _raw("backup official")
    catalog = _catalog(primary)
    entry = dict(catalog["entries"][0])  # type: ignore[index]
    primary_transport = dict(entry["transport"])  # type: ignore[index]
    primary_transport["status"] = 503
    entry["transport"] = primary_transport
    backup_hash = content_hash_bytes(backup)
    entry["observations"] = [
        {
            "role": "official",
            "source_ref": _source(
                backup,
                kind="official-snapshot",
                host="kbbi.kemdikbud.go.id",
            ),
            "transport": {
                "adapter": "fixture",
                "content": backup.decode(),
                "content_type": "text/html",
                "expected_raw_hash": backup_hash,
                "comparison_mode": "exact",
                "status": 200,
            },
        },
        {
            "role": "fallback",
            "source_ref": _source(
                backup,
                kind="fallback",
                host="kbbi.web.id",
            ),
            "transport": {
                "adapter": "fixture",
                "content": backup.decode(),
                "content_type": "text/html",
                "expected_raw_hash": backup_hash,
                "comparison_mode": "exact",
                "status": 200,
            },
        },
    ]

    driver = CheckpointDriver(root=tmp_path)
    result = driver.run(
        {**catalog, "entries": [entry]},
        limit=1,
        idempotency_key="backup-official",
    )

    outcome = result.report["outcomes"][0]
    assert result.status == "completed"
    assert outcome["outcome"] == "accepted"
    assert outcome["source_ref"]["url"].endswith("/entri/kata")
    assert outcome["source_ref"]["content_hash"] == backup_hash
    assert outcome["source_role"] == "official"
    assert outcome["official_observation"]["source_ref"]["content_hash"] == backup_hash
    assert not ReviewStore(root=tmp_path).list_open()

    attempt_history = driver.attempts(result.run_id)
    assert attempt_history["attempt_count"] == 3
    assert [
        attempt["source_kind"] for attempt in attempt_history["physical_attempts"]
    ] == ["official-snapshot", "official-snapshot", "fallback"]
    assert attempt_history["physical_attempts"][0]["outcome"] == "retryable"
    assert attempt_history["physical_attempts"][1]["outcome"] == "accepted"
    assert attempt_history["physical_attempts"][2]["outcome"] == "accepted"


def test_attempt_history_is_physical_and_keeps_logical_rows_separate(
    tmp_path: Path,
) -> None:
    official = _raw()
    fallback = _raw("makna cermin")
    result = CheckpointDriver(root=tmp_path).run(
        _catalog(official, fallback),
        limit=1,
        idempotency_key="physical-attempts",
    )

    report = result.report
    assert report["outcome_counts"]["quarantined"] == 1
    assert report["selected_count"] == 1
    assert report["current_outcome_count"] == 1
    assert report["logical_attempt_count"] == 1
    assert report["attempt_count"] == 2
    assert report["physical_attempt_count"] == 2
    assert [attempt["source_kind"] for attempt in report["physical_attempts"]] == [
        "official-snapshot",
        "fallback",
    ]
    assert [attempt["sequence"] for attempt in report["physical_attempts"]] == [1, 2]
    assert all(
        attempt["run_id"] == result.run_id for attempt in report["physical_attempts"]
    )
    assert report["physical_attempts"][0]["canonical_content_hash"]
    assert report["physical_attempts"][1]["canonical_content_hash"]
    assert report["physical_attempts"][1]["conflict_result"] == "conflict"

    attempt_history = CheckpointDriver(root=tmp_path).attempts(result.run_id)
    assert attempt_history["logical_attempt_count"] == 1
    assert len(attempt_history["attempts"]) == 1
    assert len(attempt_history["physical_attempts"]) == 2
    assert (
        attempt_history["attempts"][0]["source_attempts"]
        == (attempt_history["physical_attempts"])
    )


def test_candidate_requires_explicit_release_approval(tmp_path: Path) -> None:
    result = CheckpointDriver(root=tmp_path).run(
        _catalog(_raw()),
        limit=1,
        idempotency_key="candidate-approval",
    )
    driver = CheckpointDriver(root=tmp_path)
    pending = driver.evaluate_candidate(result.run_id)
    assert pending["eligible"] is False
    assert "release_approval_required" in pending["reason_codes"]
    approved = driver.evaluate_candidate(
        result.run_id,
        release_approved=True,
        release_reviewer="release-operator",
        release_reason="Fixture checkpoint reviewed.",
    )
    assert approved["eligible"] is False
    assert approved["candidate_created"] is False
    assert approved["excluded"] == []
    assert "checkpoint_incomplete" in approved["reason_codes"]


def test_candidate_approval_requires_human_metadata(tmp_path: Path) -> None:
    result = CheckpointDriver(root=tmp_path).run(
        _catalog(_raw()),
        limit=1,
        idempotency_key="candidate-metadata",
    )
    evaluation = CheckpointDriver(root=tmp_path).evaluate_candidate(
        result.run_id,
        release_approved=True,
    )
    assert evaluation["eligible"] is False
    assert evaluation["candidate_created"] is False
    assert "release_reviewer_required" in evaluation["reason_codes"]


def test_raw_snapshot_store_keeps_hash_identity_and_rejects_collision(
    tmp_path: Path,
) -> None:
    raw = b"immutable raw"
    source = SourceRef(
        **_source(raw, kind="official-snapshot", host="kbbi.kemdikbud.go.id")
    )
    store = RawSnapshotStore(tmp_path)
    first = store.put(raw, source)
    second = store.put(raw, source)
    assert first["raw_snapshot_id"] == second["raw_snapshot_id"]
    assert first["observation_id"] == second["observation_id"]
    with pytest.raises(ValueError, match="hash mismatch"):
        store.put(b"different bytes", source)
