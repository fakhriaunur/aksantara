"""Deterministic processing of one checkpoint catalog record."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from aksantara.domain.errors import QuarantinedError, ValidationError
from aksantara.domain.provenance import (
    CANONICAL_CONTENT_FIELDS,
    canonical_content_hash,
    content_hash_bytes,
)
from aksantara.ingest.checkpoint_authority import _source_identity
from aksantara.ingest.checkpoint_catalog import normalize_stable_key
from aksantara.ingest.checkpoint_storage import (
    _safe_relative,
    _write_immutable,
    _write_json,
)
from aksantara.ingest.checkpoint_types import (
    CHECKPOINT_SCHEMA_VERSION,
    CatalogValidationError,
    _CatalogRecord,
)
from aksantara.ingest.snapshots import RawSnapshotStore
from aksantara.parse.parser_contract import ParserError, parse_kbbi
from aksantara.validate.schema import validate_entry


class _CheckpointDriver(Protocol):
    root: Path

    def _ordered_bindings(self, record: _CatalogRecord) -> list[dict[str, Any]]: ...

    def _process_without_official(
        self,
        record: _CatalogRecord,
        *,
        bindings: list[dict[str, Any]],
        run_dir: Path,
        selected_index: int,
        reason: str,
        initial_attempt: dict[str, Any],
        final_outcome: str = "quarantined",
        final_exclusion_reason: str = "official_required",
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def _fixture_bytes_for_transport(self, transport: Mapping[str, Any]) -> bytes: ...

    def _process_additional_observations(
        self,
        record: _CatalogRecord,
        *,
        run_dir: Path,
        selected_index: int,
        official_entry: Any,
        canonical_hash: str,
        bindings: list[dict[str, Any]] | None = None,
        official_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def process_record(
    driver: _CheckpointDriver,
    record: _CatalogRecord,
    run_dir: Path,
    selected_index: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bindings = driver._ordered_bindings(record)
    canonical_binding = bindings[0]
    source_ref = canonical_binding["source_ref"]
    transport = canonical_binding["transport"]
    source_role = str(canonical_binding.get("role", "official"))
    base_attempt: dict[str, Any] = {
        "stable_key": record.stable_key,
        "selected_index": selected_index,
        "attempt": 1,
        "transport_attempt": 1,
        "validation_attempt": 0,
        "adapter": transport["adapter"],
        "status": transport["status"],
        "source_kind": source_ref.source_kind,
        "source_role": source_role,
        "retry_decision": False,
        "outcome": "pending",
    }
    # A lower-authority binding can never become canonical, even when it
    # is the only binding or when an official binding is unavailable.
    # Keep all bindings in the attempt/evidence ledger so an operator can
    # prove that official access was tried before fallback evidence.
    if source_ref.source_kind not in {"official-live", "official-snapshot"}:
        return driver._process_without_official(
            record,
            bindings=bindings,
            run_dir=run_dir,
            selected_index=selected_index,
            reason="official_required",
            initial_attempt=base_attempt,
        )
    if (
        source_ref.source_kind in {"official-live", "official-snapshot"}
        and transport["status"] >= 400
        and not (transport["status"] == 429 or transport["status"] >= 500)
        and len(bindings) == 1
    ):
        reason = f"fixture transport status {transport['status']}"
        base_attempt.update(
            {
                "outcome": "failed",
                "error": {"code": "transport_permanent", "message": reason},
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "failed",
                "reason": reason,
                "exclusion_reason": "transport_permanent",
                "attempt_count": 1,
                "source_ref": _source_identity(source_ref),
                "candidate_namespace": False,
            },
            base_attempt,
        )
    if (transport["status"] == 429 or transport["status"] >= 500) and len(bindings) > 1:
        return driver._process_without_official(
            record,
            bindings=bindings,
            run_dir=run_dir,
            selected_index=selected_index,
            reason="official_transport_retryable",
            initial_attempt=base_attempt,
            final_outcome="retryable",
            final_exclusion_reason="transport_retryable",
        )
    if transport["status"] >= 400 and not (
        transport["status"] == 429 or transport["status"] >= 500
    ):
        return driver._process_without_official(
            record,
            bindings=bindings,
            run_dir=run_dir,
            selected_index=selected_index,
            reason="official_transport_failure",
            initial_attempt=base_attempt,
        )
    if transport["status"] == 429 or transport["status"] >= 500:
        reason = f"fixture transport status {transport['status']}"
        base_attempt.update(
            {
                "retry_decision": True,
                "outcome": "retryable",
                "error": {"code": "transport_retryable", "message": reason},
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "retryable",
                "reason": reason,
                "exclusion_reason": "transport_retryable",
                "attempt_count": 1,
                "source_ref": _source_identity(source_ref),
                "candidate_namespace": False,
            },
            base_attempt,
        )
    if transport["status"] >= 400:
        reason = f"fixture transport status {transport['status']}"
        base_attempt.update(
            {
                "outcome": "failed",
                "error": {"code": "transport_permanent", "message": reason},
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "failed",
                "reason": reason,
                "exclusion_reason": "transport_permanent",
                "attempt_count": 1,
                "source_ref": _source_identity(source_ref),
                "candidate_namespace": False,
            },
            base_attempt,
        )

    try:
        raw_bytes = driver._fixture_bytes_for_transport(transport)
    except (CatalogValidationError, OSError) as exc:
        if len(bindings) > 1:
            return driver._process_without_official(
                record,
                bindings=bindings,
                run_dir=run_dir,
                selected_index=selected_index,
                reason="official_transport_failure",
                initial_attempt=base_attempt,
            )
        reason = str(exc)
        base_attempt.update(
            {
                "outcome": "rejected",
                "error": {"code": "fixture_read_error", "message": reason},
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "rejected",
                "reason": reason,
                "exclusion_reason": "fixture_read_error",
                "attempt_count": 1,
                "source_ref": _source_identity(source_ref),
                "candidate_namespace": False,
            },
            base_attempt,
        )
    actual_hash = content_hash_bytes(raw_bytes)
    expected_hash = str(transport["expected_raw_hash"])
    if not expected_hash or actual_hash != expected_hash:
        if len(bindings) > 1:
            return driver._process_without_official(
                record,
                bindings=bindings,
                run_dir=run_dir,
                selected_index=selected_index,
                reason="official_hash_mismatch",
                initial_attempt=base_attempt,
            )
        reason = (
            f"raw hash mismatch: expected {expected_hash or '<missing>'} "
            f"actual {actual_hash}"
        )
        base_attempt.update(
            {
                "outcome": "rejected",
                "error": {
                    "code": "raw_hash_mismatch",
                    "message": reason,
                    "expected": expected_hash,
                    "actual": actual_hash,
                },
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "rejected",
                "reason": reason,
                "exclusion_reason": "raw_hash_mismatch",
                "attempt_count": 1,
                "raw_hash": actual_hash,
                "source_ref": _source_identity(source_ref),
            },
            base_attempt,
        )
    raw_path = run_dir / "raw" / f"{actual_hash}.bin"
    _write_immutable(raw_path, raw_bytes, driver.root)
    base_attempt["raw_hash"] = actual_hash
    base_attempt["validation_attempt"] = 1
    raw_store = RawSnapshotStore(driver.root)
    try:
        raw_observation = raw_store.put(
            raw_bytes,
            source_ref,
            expected_raw_hash=expected_hash,
            role="official",
        )
    except (OSError, ValueError) as exc:
        reason = str(exc)
        base_attempt.update(
            {
                "outcome": "rejected",
                "error": {
                    "code": "raw_snapshot_persistence_failure",
                    "message": reason,
                },
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "rejected",
                "reason": reason,
                "exclusion_reason": "raw_snapshot_persistence_failure",
                "attempt_count": 1,
                "raw_hash": actual_hash,
                "raw_reference": _safe_relative(driver.root, raw_path),
                "source_ref": _source_identity(source_ref),
                "candidate_namespace": False,
            },
            base_attempt,
        )
    try:
        entry = parse_kbbi(raw_bytes, source_ref)
        validate_entry(entry, raw_bytes=raw_bytes)
        if (
            normalize_stable_key(entry.id) != record.stable_key
            and normalize_stable_key(entry.lema) != record.stable_key
        ):
            raise ValidationError("parsed entry identity does not match stable_key")
    except QuarantinedError as exc:
        if len(bindings) > 1:
            return driver._process_without_official(
                record,
                bindings=bindings,
                run_dir=run_dir,
                selected_index=selected_index,
                reason=getattr(exc, "reason", "official_validation_failure"),
                initial_attempt=base_attempt,
            )
        reason = str(exc)
        base_attempt.update(
            {
                "outcome": "quarantined",
                "error": {
                    "code": exc.reason,
                    "message": reason,
                },
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "quarantined",
                "reason": reason,
                "exclusion_reason": getattr(exc, "reason", "quarantined"),
                "attempt_count": 1,
                "raw_hash": actual_hash,
                "raw_reference": _safe_relative(driver.root, raw_path),
                "source_ref": _source_identity(source_ref),
                "raw_snapshot_id": raw_observation["raw_snapshot_id"],
                "observation_id": raw_observation["observation_id"],
                "candidate_namespace": False,
            },
            base_attempt,
        )
    except (ParserError, ValidationError, ValueError, TypeError) as exc:
        if len(bindings) > 1:
            return driver._process_without_official(
                record,
                bindings=bindings,
                run_dir=run_dir,
                selected_index=selected_index,
                reason="official_validation_failure",
                initial_attempt=base_attempt,
            )
        reason = str(exc)
        base_attempt.update(
            {
                "outcome": "rejected",
                "error": {
                    "code": "deterministic_validation_failure",
                    "message": reason,
                },
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "rejected",
                "reason": reason,
                "exclusion_reason": "deterministic_validation_failure",
                "attempt_count": 1,
                "raw_hash": actual_hash,
                "raw_reference": _safe_relative(driver.root, raw_path),
                "source_ref": _source_identity(source_ref),
                "raw_snapshot_id": raw_observation["raw_snapshot_id"],
                "observation_id": raw_observation["observation_id"],
                "candidate_namespace": False,
            },
            base_attempt,
        )
    canonical_payload = entry.model_dump(mode="json")
    canonical_hash = canonical_content_hash(entry)
    parsed_path = run_dir / "parsed" / f"{record.stable_key.replace(' ', '_')}.json"
    _write_json(
        parsed_path,
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "stable_key": record.stable_key,
            "entry": canonical_payload,
            "canonical_content_hash": canonical_hash,
            "canonical_serialization": {
                "algorithm": "canonical-content-v1",
                "fields": list(CANONICAL_CONTENT_FIELDS),
            },
            "candidate_namespace": False,
        },
        driver.root,
    )
    base_attempt.update(
        {
            "outcome": "accepted",
            "canonical_content_hash": canonical_hash,
        }
    )
    observations: list[dict[str, Any]] = [
        {
            "role": "official",
            "source_kind": source_ref.source_kind,
            "source_ref": _source_identity(source_ref),
            "raw_sha256": actual_hash,
            "raw_snapshot_id": raw_observation["raw_snapshot_id"],
            "observation_id": raw_observation["observation_id"],
            "canonical_content_hash": canonical_hash,
        }
    ]
    fallback_observations = driver._process_additional_observations(
        record,
        run_dir=run_dir,
        selected_index=selected_index,
        official_entry=entry,
        canonical_hash=canonical_hash,
        bindings=bindings[1:],
        official_observation=observations[0],
    )
    observations.extend(fallback_observations["observations"])
    conflict = fallback_observations.get("conflict")
    if conflict is not None:
        # Keep the official parse and both immutable source sides, but
        # quarantine the item so no candidate can consume it.
        base_attempt.update(
            {
                "outcome": "quarantined",
                "error": {
                    "code": "lexical_conflict",
                    "message": "official and fallback lexical fields differ",
                    "conflict_id": conflict["conflict_id"],
                },
            }
        )
        return (
            {
                "stable_key": record.stable_key,
                "selected_index": selected_index,
                "outcome": "quarantined",
                "reason": "official and fallback lexical fields differ",
                "exclusion_reason": "lexical_conflict",
                "attempt_count": len(observations),
                "raw_hash": actual_hash,
                "canonical_content_hash": canonical_hash,
                "raw_reference": _safe_relative(driver.root, raw_path),
                "parsed_reference": _safe_relative(driver.root, parsed_path),
                "source_ref": _source_identity(source_ref),
                "raw_snapshot_id": raw_observation["raw_snapshot_id"],
                "observation_id": raw_observation["observation_id"],
                "entry_id": entry.id,
                "lema": entry.lema,
                "candidate_namespace": False,
                "observations": observations,
                "conflict_id": conflict["conflict_id"],
                "review_status": conflict["review_status"],
                "release_blocking": True,
            },
            base_attempt,
        )
    return (
        {
            "stable_key": record.stable_key,
            "selected_index": selected_index,
            "outcome": "accepted",
            "reason": "parsed_and_validated",
            "attempt_count": 1,
            "raw_hash": actual_hash,
            "canonical_content_hash": canonical_hash,
            "raw_reference": _safe_relative(driver.root, raw_path),
            "parsed_reference": _safe_relative(driver.root, parsed_path),
            "source_ref": _source_identity(source_ref),
            "raw_snapshot_id": raw_observation["raw_snapshot_id"],
            "observation_id": raw_observation["observation_id"],
            "entry_id": entry.id,
            "lema": entry.lema,
            "candidate_namespace": False,
            "observations": observations,
            "review_status": "approved",
            "release_blocking": False,
        },
        base_attempt,
    )
