from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aksantara.api.routes import create_app
from aksantara.domain.provenance import content_hash_bytes
from aksantara.ingest.checkpoint import (
    CatalogValidationError,
    CheckpointConflictError,
    CheckpointDriver,
    LimitValidationError,
    normalize_stable_key,
)


def _catalog(tmp_path: Path, count: int = 120) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for index in range(count):
        key = f"entry-{index:03d}"
        raw = (
            f"<entry><h1>{key}</h1><p class='makna'>definisi {key}</p></entry>".encode()
        )
        source_hash = content_hash_bytes(raw)
        relative_path = Path("fixtures") / f"{key}.html"
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        entries.append(
            {
                "stable_key": key,
                "source_ref": {
                    "url": f"https://kbbi.kemdikbud.go.id/entri/{key}",
                    "source_kind": "official-snapshot",
                    "edition": "VI",
                    "source_version": "fixture-v1",
                    "retrieved_at": "2026-08-31T00:00:00Z",
                    "content_hash": source_hash,
                    "parser_version": "0.1.0",
                },
                "transport": {
                    "adapter": "fixture",
                    "path": str(relative_path),
                    "content_type": "text/html",
                    "expected_raw_hash": source_hash,
                    "comparison_mode": "exact",
                    "status": 200,
                },
            }
        )
    return {
        "catalog_id": "checkpoint-fixture-v1",
        "corpus_version": "kbbi-vi-fixture-v1",
        "entries": entries,
    }


def test_normalization_rejects_unsafe_keys() -> None:
    assert normalize_stable_key("  Entry\u00a0  One ") == "entry one"
    with pytest.raises(CatalogValidationError):
        normalize_stable_key("../escape")
    with pytest.raises(CatalogValidationError):
        normalize_stable_key("")
    with pytest.raises(CatalogValidationError):
        normalize_stable_key("entry\none")


def test_driver_selects_exactly_100_and_rerun_is_idempotent(tmp_path: Path) -> None:
    manifest = _catalog(tmp_path)
    driver = CheckpointDriver(root=tmp_path)

    first = driver.run(manifest, limit=100, idempotency_key="same-run")
    report = driver.report(first.run_id)

    assert first.status == "completed"
    assert report["selected_count"] == 100
    assert report["selected_keys"] == [f"entry-{i:03d}" for i in range(100)]
    assert report["outcome_counts"]["accepted"] == 100
    assert report["attempt_count"] == 100
    assert report["network_trace"]["live_network_attempts"] == 0
    assert report["network_trace"]["source_reads"] == 100
    assert report["network_trace"]["unselected_reads"] == 0
    assert report["completion"]["checkpoint_complete"] is True
    assert report["eligibility"]["eligible"] is False
    assert report["promotion"]["pointer_changed"] is False

    second = driver.run(manifest, limit=100, idempotency_key="same-run")
    assert second.run_id == first.run_id
    assert driver.report(second.run_id)["revision"] == report["revision"]


def test_lower_limit_is_processed_but_not_a_complete_fixed_checkpoint(
    tmp_path: Path,
) -> None:
    result = CheckpointDriver(root=tmp_path).run(
        _catalog(tmp_path, count=3),
        limit=3,
        idempotency_key="short-limit",
    )
    report = result.report
    assert result.status == "completed"
    assert report["completion"]["checkpoint_complete"] is False
    assert report["eligibility"]["eligible"] is False
    assert "effective_limit_below_fixed_100" in report["completion"]["reasons"]


def test_empty_idempotency_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CheckpointConflictError):
        CheckpointDriver(root=tmp_path).run(
            _catalog(tmp_path, count=1),
            limit=1,
            idempotency_key="",
        )
    assert not (tmp_path / ".aksantara").exists()


def test_invalid_source_identity_is_a_catalog_error(tmp_path: Path) -> None:
    manifest = _catalog(tmp_path, count=1)
    entry = dict(manifest["entries"][0])  # type: ignore[index]
    source_ref = dict(entry["source_ref"])  # type: ignore[index]
    source_ref["source_kind"] = "not-a-source-kind"
    entry["source_ref"] = source_ref
    with pytest.raises(CatalogValidationError):
        CheckpointDriver(root=tmp_path).preflight(
            {**manifest, "entries": [entry]},
            limit=1,  # type: ignore[arg-type]
        )


def test_equivalent_input_order_has_same_release_fingerprint(tmp_path: Path) -> None:
    manifest = _catalog(tmp_path)
    reordered = {**manifest, "entries": list(reversed(manifest["entries"]))}  # type: ignore[arg-type]
    driver = CheckpointDriver(root=tmp_path)

    left = driver.preflight(manifest, limit=100)
    right = driver.preflight(reordered, limit=100)

    assert left.catalog_fingerprint == right.catalog_fingerprint
    assert left.run_fingerprint == right.run_fingerprint
    assert left.selected_keys == right.selected_keys


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_limit_bounds_fail_before_run(tmp_path: Path, limit: int) -> None:
    with pytest.raises(LimitValidationError):
        CheckpointDriver(root=tmp_path).preflight(_catalog(tmp_path), limit=limit)
    assert not (tmp_path / "runs").exists()


def test_duplicate_and_root_escape_fail_before_source_processing(
    tmp_path: Path,
) -> None:
    manifest = _catalog(tmp_path, count=2)
    entries = list(manifest["entries"])  # type: ignore[arg-type]
    entries[1] = {**entries[1], "stable_key": " ENTRY-000 "}
    with pytest.raises(CatalogValidationError):
        CheckpointDriver(root=tmp_path).run(
            {**manifest, "entries": entries},
            limit=2,  # type: ignore[arg-type]
        )
    assert not (tmp_path / "runs").exists()

    manifest = _catalog(tmp_path, count=1)
    entry = dict(manifest["entries"][0])  # type: ignore[index]
    transport = dict(entry["transport"])  # type: ignore[index]
    transport["path"] = "../outside.html"
    entry["transport"] = transport
    with pytest.raises(CatalogValidationError):
        CheckpointDriver(root=tmp_path).run(
            {**manifest, "entries": [entry]},
            limit=1,  # type: ignore[arg-type]
        )
    assert not (tmp_path / "runs").exists()


def test_api_checkpoint_operations_are_documented_and_machine_readable(
    tmp_path: Path,
) -> None:
    manifest = _catalog(tmp_path, count=3)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = TestClient(create_app())

    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    assert "/checkpoints/runs" in paths
    assert "/checkpoints/runs/{run_id}" in paths
    assert "/checkpoints/runs/{run_id}/report" in paths

    created = client.post(
        "/checkpoints/runs",
        json={
            "catalog_path": str(catalog_path),
            "root": str(tmp_path),
            "limit": 3,
            "idempotency_key": "api-run",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["run_id"]
    assert body["fingerprints"]["catalog"]
    assert body["references"]["report"]
    assert body["promotion"]["pointer_changed"] is False

    status = client.get(f"/checkpoints/runs/{body['run_id']}")
    assert status.status_code == 200
    assert status.json()["selected_count"] == 3

    report = client.get(f"/checkpoints/runs/{body['run_id']}/report")
    assert report.status_code == 200
    assert report.json()["outcome_counts"]["accepted"] == 3
