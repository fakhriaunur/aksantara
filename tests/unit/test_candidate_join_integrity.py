from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aksantara.domain.provenance import content_hash_bytes
from aksantara.ingest.checkpoint import CheckpointDriver


def _catalog(root: Path, count: int = 100) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for index in range(count):
        stable_key = f"entry-{index:03d}"
        raw = (
            f"<article><h1>{stable_key}</h1>"
            f"<p class='makna'>definisi {stable_key}</p></article>"
        ).encode()
        digest = content_hash_bytes(raw)
        fixture = root / "fixtures" / f"{stable_key}.html"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_bytes(raw)
        entries.append(
            {
                "stable_key": stable_key,
                "source_ref": {
                    "url": f"https://kbbi.kemdikbud.go.id/entri/{stable_key}",
                    "source_kind": "official-snapshot",
                    "edition": "VI",
                    "source_version": "fixture-v1",
                    "retrieved_at": "2026-08-31T00:00:00Z",
                    "content_hash": digest,
                    "parser_version": "0.1.0",
                },
                "transport": {
                    "adapter": "fixture",
                    "path": str(fixture.relative_to(root)),
                    "content_type": "text/html",
                    "expected_raw_hash": digest,
                    "comparison_mode": "exact",
                    "status": 200,
                },
            }
        )
    return {
        "catalog_id": "candidate-join-fixture-v1",
        "corpus_version": "kbbi-vi-candidate-join-v1",
        "entries": entries,
    }


def _run(root: Path) -> tuple[CheckpointDriver, str]:
    driver = CheckpointDriver(root=root)
    result = driver.run(
        _catalog(root),
        limit=100,
        idempotency_key="candidate-join-run",
    )
    assert result.status == "completed"
    return driver, result.run_id


def _first_outcome_path(root: Path, run_id: str) -> Path:
    return root / ".aksantara" / "checkpoint-runs" / run_id / "outcomes.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def test_candidate_rejects_missing_observation_without_candidate_write(
    tmp_path: Path,
) -> None:
    driver, run_id = _run(tmp_path)
    outcomes_path = _first_outcome_path(tmp_path, run_id)
    outcomes = _read_json(outcomes_path)
    outcomes["outcomes"][0]["observation_id"] = None
    _write_json(outcomes_path, outcomes)

    evaluation = driver.evaluate_candidate(
        run_id,
        release_approved=True,
        release_reviewer="release-operator",
        release_reason="candidate join fixture",
    )

    assert evaluation["eligible"] is False
    assert evaluation["candidate_created"] is False
    assert evaluation["excluded"][0]["reason"] == "report_outcome_mismatch"
    assert not (tmp_path / ".aksantara" / "candidates").exists()


def test_candidate_rejects_observation_not_joined_to_current_run(
    tmp_path: Path,
) -> None:
    driver, run_id = _run(tmp_path)
    run_dir = tmp_path / ".aksantara" / "checkpoint-runs" / run_id
    attempts_path = run_dir / "attempts.json"
    attempts = _read_json(attempts_path)
    removed = attempts["physical_attempts"].pop(0)
    attempts["attempts"][0]["source_attempts"].pop(0)
    attempts["attempt_count"] -= 1
    attempts["physical_attempt_count"] -= 1
    _write_json(attempts_path, attempts)

    evaluation = driver.evaluate_candidate(
        run_id,
        release_approved=True,
        release_reviewer="release-operator",
        release_reason="candidate join fixture",
    )

    assert evaluation["eligible"] is False
    assert evaluation["candidate_created"] is False
    assert evaluation["excluded"][0]["reason"] == "observation_attempt_missing"
    assert removed["run_id"] == run_id


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("transform_pin", "transform_pin_mismatch"),
        ("validation_pin", "validation_policy_mismatch"),
        ("run_fingerprint", "run_fingerprint_mismatch"),
    ],
)
def test_candidate_rejects_parsed_artifact_pin_and_run_drift(
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    driver, run_id = _run(tmp_path)
    report = driver.report(run_id)
    first = report["outcomes"][0]
    parsed_path = tmp_path / first["parsed_reference"]
    parsed = _read_json(parsed_path)
    if mutation == "transform_pin":
        parsed["pins"]["transform_version"] = "tampered-transform"
    elif mutation == "validation_pin":
        parsed["pins"]["validation_policy"] = "tampered-policy"
    else:
        parsed["lineage"]["run_fingerprint"] = "f" * 64
    _write_json(parsed_path, parsed)

    evaluation = driver.evaluate_candidate(
        run_id,
        release_approved=True,
        release_reviewer="release-operator",
        release_reason="candidate join fixture",
    )

    assert evaluation["eligible"] is False
    assert evaluation["candidate_created"] is False
    assert evaluation["excluded"][0]["reason"] == expected_reason
    assert not (tmp_path / ".aksantara" / "candidates").exists()


@pytest.mark.parametrize(
    ("artifact", "mutation", "expected_reason"),
    [
        ("preflight", "pins", "run_fingerprint_mismatch"),
        ("report", "fingerprints", "run_fingerprint_mismatch"),
        ("attempts", "logical", "observation_attempt_mismatch"),
    ],
)
def test_candidate_rejects_durable_run_lineage_drift(
    tmp_path: Path,
    artifact: str,
    mutation: str,
    expected_reason: str,
) -> None:
    driver, run_id = _run(tmp_path)
    run_dir = tmp_path / ".aksantara" / "checkpoint-runs" / run_id
    path = run_dir / f"{artifact}.json"
    payload = _read_json(path)
    if mutation == "pins":
        payload["pins"]["transform_version"] = "tampered-transform"
    elif mutation == "fingerprints":
        payload["fingerprints"]["run"] = "f" * 64
    else:
        payload["attempts"][0]["run_fingerprint"] = "f" * 64
    _write_json(path, payload)

    evaluation = driver.evaluate_candidate(
        run_id,
        release_approved=True,
        release_reviewer="release-operator",
        release_reason="candidate join fixture",
    )

    assert evaluation["eligible"] is False
    assert evaluation["candidate_created"] is False
    assert evaluation["excluded"][0]["reason"] == expected_reason
    assert not (tmp_path / ".aksantara" / "candidates").exists()


def test_candidate_rejects_canonical_serialization_contract_drift(
    tmp_path: Path,
) -> None:
    driver, run_id = _run(tmp_path)
    report = driver.report(run_id)
    first = report["outcomes"][0]
    parsed_path = tmp_path / first["parsed_reference"]
    parsed = _read_json(parsed_path)
    parsed["canonical_serialization"]["sort_keys"] = False
    _write_json(parsed_path, parsed)

    evaluation = driver.evaluate_candidate(
        run_id,
        release_approved=True,
        release_reviewer="release-operator",
        release_reason="candidate join fixture",
    )

    assert evaluation["eligible"] is False
    assert evaluation["candidate_created"] is False
    assert evaluation["excluded"][0]["reason"] == "canonical_serialization_mismatch"
    assert not (tmp_path / ".aksantara" / "candidates").exists()
