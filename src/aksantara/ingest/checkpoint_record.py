"""Deterministic processing of one checkpoint catalog record."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from aksantara.domain.provenance import CANONICAL_RECORD_FIELDS, canonical_record_bytes
from aksantara.ingest.checkpoint_authority import _source_identity
from aksantara.ingest.checkpoint_storage import (
    _read_json,
    _safe_relative,
    _write_immutable,
    _write_json,
)
from aksantara.ingest.checkpoint_types import (
    AUTHORITY_POLICY_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    _CatalogRecord,
)
from aksantara.validate.conflicts import lexical_field_diffs
from aksantara.validate.review import ReviewStore


class _CheckpointDriver(Protocol):
    root: Path

    def _ordered_bindings(self, record: _CatalogRecord) -> list[dict[str, Any]]: ...

    def _observe_binding(
        self,
        record: _CatalogRecord,
        *,
        binding: Mapping[str, Any],
        run_dir: Path,
        selected_index: int,
        binding_index: int,
    ) -> dict[str, Any]: ...


def process_record(
    driver: _CheckpointDriver,
    record: _CatalogRecord,
    run_dir: Path,
    selected_index: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return _process_record_with_observation_lineage(
        driver,
        record,
        run_dir,
        selected_index,
    )


def _process_record_with_observation_lineage(
    driver: _CheckpointDriver,
    record: _CatalogRecord,
    run_dir: Path,
    selected_index: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Process every configured binding and select the first valid official.

    A catalog entry is one logical key, but each configured source binding is
    a physical observation.  The two ledgers are intentionally separate:
    ``source_attempts`` contains one durable row per physical observation,
    while the returned outcome contains one current row for the logical key.
    """
    bindings = driver._ordered_bindings(record)
    observations: list[dict[str, Any]] = []
    physical_attempts: list[dict[str, Any]] = []
    official_results: list[dict[str, Any]] = []
    evidence_results: list[dict[str, Any]] = []

    official_bindings = [
        (binding_index, binding)
        for binding_index, binding in enumerate(bindings)
        if binding["source_ref"].source_kind in {"official-live", "official-snapshot"}
    ]
    evidence_bindings = [
        (binding_index, binding)
        for binding_index, binding in enumerate(bindings)
        if binding["source_ref"].source_kind
        not in {"official-live", "official-snapshot"}
    ]
    for binding_index, binding in official_bindings:
        result = driver._observe_binding(
            record,
            binding=binding,
            run_dir=run_dir,
            selected_index=selected_index,
            binding_index=binding_index,
        )
        _append_physical_result(result, physical_attempts, observations)
        official_results.append(result)

    selected_official: dict[str, Any] | None = None
    for result in official_results:
        if result["entry"] is not None and result["attempt"]["outcome"] == "accepted":
            selected_official = result
            break

    for binding_index, binding in evidence_bindings:
        result = driver._observe_binding(
            record,
            binding=binding,
            run_dir=run_dir,
            selected_index=selected_index,
            binding_index=binding_index,
        )
        _append_physical_result(result, physical_attempts, observations)
        evidence_results.append(result)

    preflight_payload = _read_json(run_dir / "preflight.json")
    preflight_fingerprints = preflight_payload.get("fingerprints", {})
    preflight_pins = preflight_payload.get("pins", {})
    if not isinstance(preflight_fingerprints, Mapping) or not isinstance(
        preflight_pins, Mapping
    ):
        raise ValueError("durable preflight lineage metadata is malformed")
    for attempt in physical_attempts:
        attempt["catalog_fingerprint"] = preflight_fingerprints.get("catalog")
        attempt["run_fingerprint"] = preflight_fingerprints.get("run")
        attempt["pins"] = {
            "parser_version": preflight_pins.get("parser_version"),
            "transform_version": preflight_pins.get("transform_version"),
            "validation_policy": preflight_pins.get("validation_policy"),
        }

    if selected_official is None:
        return _finish_without_official(
            driver,
            record,
            run_dir=run_dir,
            selected_index=selected_index,
            bindings=bindings,
            observations=observations,
            physical_attempts=physical_attempts,
            official_results=official_results,
            evidence_results=evidence_results,
        )

    winner_entry = selected_official["entry"]
    winner_attempt = selected_official["attempt"]
    winner_attempt["selection_result"] = "selected_official"
    selected_official["observation"]["selection_result"] = "selected_official"
    for result in official_results:
        if result is not selected_official:
            result["attempt"]["selection_result"] = "official_not_selected"
            result["observation"]["selection_result"] = "official_not_selected"

    canonical_hash = str(winner_attempt["canonical_content_hash"])
    parsed_path = run_dir / "parsed" / f"{record.stable_key.replace(' ', '_')}.json"
    canonical_path = (
        run_dir / "canonical" / f"{record.stable_key.replace(' ', '_')}.json"
    )
    canonical_payload = winner_entry.model_dump(mode="json")
    _write_immutable(
        canonical_path,
        canonical_record_bytes(winner_entry),
        driver.root,
    )
    attempt = selected_official["attempt"]
    source_identity = _source_identity(selected_official["source_ref"])
    lineage = {
        "run_id": run_dir.name,
        "stable_key": record.stable_key,
        "attempt_id": attempt["attempt_id"],
        "catalog_fingerprint": preflight_fingerprints.get("catalog"),
        "run_fingerprint": preflight_fingerprints.get("run"),
        "pins": {
            "parser_version": preflight_pins.get("parser_version"),
            "transform_version": preflight_pins.get("transform_version"),
            "validation_policy": preflight_pins.get("validation_policy"),
        },
        "source_ref": source_identity,
        "source_role": "official",
        "authority_role": "official",
        "raw_hash": attempt.get("raw_hash"),
        "raw_content_hash": attempt.get("raw_content_hash"),
        "raw_snapshot_id": attempt.get("raw_snapshot_id"),
        "observation_id": attempt.get("observation_id"),
        "raw_reference": attempt.get("raw_reference"),
        "canonical_reference": _safe_relative(driver.root, canonical_path),
        "canonical_content_hash": canonical_hash,
        "entry_id": winner_entry.id,
    }
    _write_json(
        parsed_path,
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": run_dir.name,
            "stable_key": record.stable_key,
            "entry": canonical_payload,
            "canonical_content_hash": canonical_hash,
            "fingerprints": {
                "catalog": preflight_fingerprints.get("catalog"),
                "run": preflight_fingerprints.get("run"),
            },
            "pins": {
                "parser_version": preflight_pins.get("parser_version"),
                "transform_version": preflight_pins.get("transform_version"),
                "validation_policy": preflight_pins.get("validation_policy"),
            },
            "lineage": lineage,
            "canonical_serialization": {
                "algorithm": "canonical-record-v1",
                "fields": list(CANONICAL_RECORD_FIELDS),
                "encoding": "UTF-8",
                "separators": [",", ":"],
                "sort_keys": True,
                "final_newline": True,
            },
            "canonical_reference": _safe_relative(driver.root, canonical_path),
            "candidate_namespace": False,
        },
        driver.root,
    )
    _annotate_observation_lineage(
        selected_official,
        parsed_reference=_safe_relative(driver.root, parsed_path),
        canonical_reference=_safe_relative(driver.root, canonical_path),
        canonical_hash=canonical_hash,
        entry_id=winner_entry.id,
        lema=winner_entry.lema,
    )

    conflict: dict[str, Any] | None = None
    review_store = ReviewStore(root=driver.root)
    for result in evidence_results:
        entry = result["entry"]
        attempt = result["attempt"]
        observation = result["observation"]
        if entry is None or attempt["outcome"] != "accepted":
            continue
        evidence_hash = str(attempt["canonical_content_hash"])
        field_diffs = lexical_field_diffs(winner_entry, entry)
        attempt["conflict_result"] = "conflict" if field_diffs else "no_conflict"
        observation["conflict_result"] = attempt["conflict_result"]
        observation["differing_fields"] = [str(item["field"]) for item in field_diffs]
        if not field_diffs:
            continue
        if conflict is not None:
            continue
        official_side = _review_side(
            selected_official,
            entry=winner_entry,
            canonical_hash=canonical_hash,
        )
        evidence_side = _review_side(
            result,
            entry=entry,
            canonical_hash=evidence_hash,
        )
        conflict = review_store.persist_conflict(
            entry_id=winner_entry.id,
            stable_key=record.stable_key,
            official=official_side,
            fallback=evidence_side,
            differing_fields=[str(item["field"]) for item in field_diffs],
            field_diffs=field_diffs,
            first_seen_run=run_dir.name,
            policy_version=AUTHORITY_POLICY_VERSION,
        )
        attempt["conflict_id"] = conflict["conflict_id"]
        observation["conflict_id"] = conflict["conflict_id"]
        selected_official["attempt"]["conflict_id"] = conflict["conflict_id"]
        selected_official["attempt"]["conflict_result"] = "conflict"
        selected_official["observation"]["conflict_id"] = conflict["conflict_id"]
        selected_official["observation"]["conflict_result"] = "conflict"

    source_attempts = _finalize_physical_attempts(
        physical_attempts,
        observations,
        conflict_id=(conflict or {}).get("conflict_id"),
    )
    logical_attempt = _logical_attempt(
        record,
        selected_index=selected_index,
        source_attempts=source_attempts,
        observations=observations,
        run_id=run_dir.name,
    )
    if conflict is not None:
        logical_attempt["outcome"] = "quarantined"
        logical_attempt["error"] = {
            "code": "lexical_conflict",
            "message": "official and fallback lexical fields differ",
            "conflict_id": conflict["conflict_id"],
        }
        logical_outcome = _accepted_outcome(
            record,
            selected_index=selected_index,
            selected_official=selected_official,
            observations=observations,
            parsed_reference=_safe_relative(driver.root, parsed_path),
            canonical_reference=_safe_relative(driver.root, canonical_path),
            canonical_hash=canonical_hash,
            source_attempts=source_attempts,
            outcome="quarantined",
            reason="official and fallback lexical fields differ",
            exclusion_reason="lexical_conflict",
            conflict=conflict,
        )
    else:
        logical_outcome = _accepted_outcome(
            record,
            selected_index=selected_index,
            selected_official=selected_official,
            observations=observations,
            parsed_reference=_safe_relative(driver.root, parsed_path),
            canonical_reference=_safe_relative(driver.root, canonical_path),
            canonical_hash=canonical_hash,
            source_attempts=source_attempts,
        )
    logical_attempt["attempt_count"] = len(source_attempts)
    logical_attempt["source_attempts"] = source_attempts
    logical_attempt["physical_attempt_count"] = len(source_attempts)
    logical_attempt["source_order"] = [
        {
            "sequence": attempt["sequence"],
            "source_kind": attempt["source_kind"],
            "role": attempt["source_role"],
        }
        for attempt in source_attempts
    ]
    return logical_outcome, logical_attempt


def _append_physical_result(
    result: dict[str, Any],
    physical_attempts: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> None:
    """Add one observation result to the ordered physical ledgers."""
    attempt = result["attempt"]
    attempt["sequence"] = len(physical_attempts) + 1
    attempt["physical_observation"] = True
    attempt["selection_result"] = "not_selected"
    attempt["conflict_result"] = "not_evaluated"
    observation = result["observation"]
    observation["sequence"] = attempt["sequence"]
    observation["selection_result"] = "not_selected"
    observations.append(observation)
    physical_attempts.append(attempt)


def _finish_without_official(
    driver: _CheckpointDriver,
    record: _CatalogRecord,
    *,
    run_dir: Path,
    selected_index: int,
    bindings: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    physical_attempts: list[dict[str, Any]],
    official_results: list[dict[str, Any]],
    evidence_results: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Close a logical row when no adapter-verified official won."""
    review_store = ReviewStore(root=driver.root)
    first_review_id: str | None = None
    for result in [*official_results, *evidence_results]:
        attempt = result["attempt"]
        if attempt["outcome"] in {"accepted", "retryable"}:
            continue
        source = dict(result["observation"])
        source["source_role"] = result["role"]
        error_code = str(attempt.get("error", {}).get("code", "official_required"))
        if result["is_official"] and error_code.startswith("transport_"):
            reason = "official_transport_failure"
        elif result["is_official"] and error_code in {
            "parse_failure",
            "deterministic_validation_failure",
        }:
            reason = "official_validation_failure"
        else:
            reason = error_code
        review = review_store.persist_quarantine(
            entry_id=record.stable_key,
            stable_key=record.stable_key,
            reason=reason,
            source=source,
            first_seen_run=run_dir.name,
            policy_version=AUTHORITY_POLICY_VERSION,
            details=str(attempt.get("error", {}).get("message", reason)),
        )
        first_review_id = first_review_id or review["review_id"]
        attempt["conflict_result"] = "quarantine"
        result["observation"]["conflict_result"] = "quarantine"
        result["observation"]["review_id"] = review["review_id"]
        attempt["review_id"] = review["review_id"]
    if first_review_id is None:
        primary = (
            observations[0]
            if observations
            else {"source_ref": bindings[0]["source_ref"].model_dump(mode="json")}
        )
        review = review_store.persist_quarantine(
            entry_id=record.stable_key,
            stable_key=record.stable_key,
            reason="official_required",
            source=dict(primary),
            first_seen_run=run_dir.name,
            policy_version=AUTHORITY_POLICY_VERSION,
            details="no adapter-verified official observation succeeded",
        )
        first_review_id = review["review_id"]
        if physical_attempts:
            physical_attempts[0]["review_id"] = first_review_id
            physical_attempts[0]["conflict_result"] = "quarantine"
        if observations:
            observations[0]["review_id"] = first_review_id
            observations[0]["conflict_result"] = "quarantine"

    source_attempts = _finalize_physical_attempts(
        physical_attempts,
        observations,
        conflict_id=None,
    )
    has_retryable = any(
        attempt["outcome"] == "retryable" for attempt in source_attempts
    )
    outcome_name = "retryable" if has_retryable else "quarantined"
    exclusion = "transport_retryable" if has_retryable else "official_required"
    reason = (
        "official transport outcome requires retry"
        if has_retryable
        else "no adapter-verified official observation succeeded"
    )
    logical_attempt = _logical_attempt(
        record,
        selected_index=selected_index,
        source_attempts=source_attempts,
        observations=observations,
        run_id=run_dir.name,
    )
    logical_attempt.update(
        {
            "outcome": outcome_name,
            "error": {
                "code": exclusion,
                "message": reason,
                "review_id": first_review_id,
            },
            "attempt_count": len(source_attempts),
            "source_attempts": source_attempts,
            "physical_attempt_count": len(source_attempts),
            "source_order": [
                {
                    "sequence": attempt["sequence"],
                    "source_kind": attempt["source_kind"],
                    "role": attempt["source_role"],
                }
                for attempt in source_attempts
            ],
        }
    )
    primary_ref = bindings[0]["source_ref"]
    logical_outcome: dict[str, Any] = {
        "stable_key": record.stable_key,
        "selected_index": selected_index,
        "outcome": outcome_name,
        "reason": reason,
        "exclusion_reason": exclusion,
        "attempt_count": len(source_attempts),
        "raw_hash": None,
        "canonical_content_hash": None,
        "source_ref": _source_identity(primary_ref),
        "source_role": str(bindings[0].get("role", "evidence")),
        "authority_role": str(bindings[0].get("role", "evidence")),
        "candidate_namespace": False,
        "observations": observations,
        "review_id": first_review_id,
        "review_status": "quarantined",
        "release_blocking": True,
    }
    return logical_outcome, logical_attempt


def _annotate_observation_lineage(
    result: dict[str, Any],
    *,
    parsed_reference: str,
    canonical_reference: str,
    canonical_hash: str,
    entry_id: str,
    lema: str,
) -> None:
    """Attach canonical artifact joins to the selected observation."""
    attempt = result["attempt"]
    observation = result["observation"]
    attempt["parsed_reference"] = parsed_reference
    attempt["canonical_reference"] = canonical_reference
    attempt["canonical_content_hash"] = canonical_hash
    observation["parsed_reference"] = parsed_reference
    observation["canonical_reference"] = canonical_reference
    observation["canonical_content_hash"] = canonical_hash
    observation["entry_id"] = entry_id
    observation["lema"] = lema


def _review_side(
    result: Mapping[str, Any],
    *,
    entry: Any,
    canonical_hash: str,
) -> dict[str, Any]:
    """Create a full immutable review side from one observation result."""
    attempt = result["attempt"]
    observation = result["observation"]
    return {
        "attempt_id": attempt["attempt_id"],
        "run_id": attempt["run_id"],
        "stable_key": observation["stable_key"],
        "entry_id": entry.id,
        "catalog_fingerprint": attempt.get("catalog_fingerprint"),
        "run_fingerprint": attempt.get("run_fingerprint"),
        "pins": dict(attempt.get("pins", {})),
        "source_ref": dict(observation["source_ref"]),
        "source_kind": observation["source_kind"],
        "source_role": observation["source_role"],
        "raw_sha256": attempt.get("raw_hash"),
        "raw_content_hash": attempt.get("raw_content_hash"),
        "raw_snapshot_id": attempt.get("raw_snapshot_id"),
        "observation_id": attempt.get("observation_id"),
        "raw_reference": attempt.get("raw_reference"),
        "parsed_reference": attempt.get("parsed_reference"),
        "canonical_reference": attempt.get("canonical_reference"),
        "canonical_content_hash": canonical_hash,
        "entry": entry.model_dump(mode="json"),
    }


def _finalize_physical_attempts(
    attempts: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    conflict_id: str | None,
) -> list[dict[str, Any]]:
    """Merge source and observation fields into stable append-only rows."""
    finalized: list[dict[str, Any]] = []
    for attempt, observation in zip(attempts, observations, strict=True):
        attempt["source_ref"] = dict(observation["source_ref"])
        attempt["source_kind"] = observation["source_kind"]
        attempt["source_role"] = observation["source_role"]
        attempt["role"] = observation["source_role"]
        attempt["authority_role"] = observation["authority_role"]
        if attempt.get("conflict_result") == "conflict":
            attempt["conflict_id"] = attempt.get("conflict_id") or conflict_id
        attempt["observation"] = {
            "raw_snapshot_id": attempt.get("raw_snapshot_id"),
            "observation_id": attempt.get("observation_id"),
            "raw_sha256": attempt.get("raw_hash"),
            "source_ref": dict(observation["source_ref"]),
            "role": observation["source_role"],
        }
        finalized.append(dict(attempt))
    return finalized


def _logical_attempt(
    record: _CatalogRecord,
    *,
    selected_index: int,
    source_attempts: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    """Build the one logical attempt row that owns physical observations."""
    first = source_attempts[0] if source_attempts else {}
    return {
        "logical_attempt_id": f"logical-attempt-{run_id}-{selected_index + 1:04d}",
        "run_id": run_id,
        "stable_key": record.stable_key,
        "selected_index": selected_index,
        "attempt": 1,
        "transport_attempt": len(source_attempts),
        "validation_attempt": sum(
            1 for attempt in source_attempts if attempt["validation_attempt"]
        ),
        "adapter": first.get("adapter"),
        "status": first.get("status"),
        "source_kind": first.get("source_kind"),
        "source_role": first.get("source_role"),
        "outcome": "pending",
        "source_attempts": source_attempts,
        "observations": observations,
    }


def _accepted_outcome(
    record: _CatalogRecord,
    *,
    selected_index: int,
    selected_official: Mapping[str, Any],
    observations: list[dict[str, Any]],
    parsed_reference: str,
    canonical_reference: str,
    canonical_hash: str,
    source_attempts: list[dict[str, Any]],
    outcome: str = "accepted",
    reason: str = "parsed_and_validated",
    exclusion_reason: str | None = None,
    conflict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one current logical outcome from the selected official join."""
    entry = selected_official["entry"]
    attempt = selected_official["attempt"]
    value: dict[str, Any] = {
        "stable_key": record.stable_key,
        "selected_index": selected_index,
        "outcome": outcome,
        "reason": reason,
        "attempt_count": len(source_attempts),
        "physical_attempt_count": len(source_attempts),
        "raw_hash": attempt.get("raw_hash"),
        "raw_content_hash": attempt.get("raw_content_hash"),
        "canonical_content_hash": canonical_hash,
        "raw_reference": attempt.get("raw_reference"),
        "parsed_reference": parsed_reference,
        "canonical_reference": canonical_reference,
        "canonical_serialization": {
            "algorithm": "canonical-record-v1",
            "fields": list(CANONICAL_RECORD_FIELDS),
            "encoding": "UTF-8",
            "final_newline": True,
        },
        "source_ref": _source_identity(selected_official["source_ref"]),
        "source_role": "official",
        "authority_role": "official",
        "raw_snapshot_id": attempt.get("raw_snapshot_id"),
        "observation_id": attempt.get("observation_id"),
        "attempt_id": attempt["attempt_id"],
        "entry_id": entry.id,
        "lema": entry.lema,
        "candidate_namespace": False,
        "observations": observations,
        "review_status": "pending" if conflict is not None else "approved",
        "release_blocking": conflict is not None,
        "official_observation": {
            "attempt_id": attempt["attempt_id"],
            "run_id": attempt["run_id"],
            "source_ref": _source_identity(selected_official["source_ref"]),
            "source_role": "official",
            "raw_snapshot_id": attempt.get("raw_snapshot_id"),
            "observation_id": attempt.get("observation_id"),
            "raw_content_hash": attempt.get("raw_content_hash"),
            "canonical_content_hash": canonical_hash,
        },
        "attempt_history_reference": "attempts.json",
    }
    if exclusion_reason is not None:
        value["exclusion_reason"] = exclusion_reason
    if conflict is not None:
        value["conflict_id"] = conflict["conflict_id"]
    return value
