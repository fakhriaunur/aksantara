"""Checkpoint execution, outcome accounting, and report generation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aksantara.domain.errors import QuarantinedError, ValidationError
from aksantara.domain.provenance import canonical_json_hash, content_hash_bytes
from aksantara.ingest.checkpoint_catalog import normalize_stable_key
from aksantara.ingest.checkpoint_storage import (
    _redact_catalog_request,
    _safe_relative,
    _write_immutable,
    _write_json,
    _write_state_json,
)
from aksantara.ingest.checkpoint_types import (
    _OUTCOMES,
    _TERMINAL_OUTCOMES,
    CHECKPOINT_SCHEMA_VERSION,
    MAX_LIMIT,
    CatalogValidationError,
    CheckpointConflictError,
    CheckpointPersistenceError,
    CheckpointPreflight,
    RunResult,
    _CatalogRecord,
)
from aksantara.parse.parser_contract import ParserError, parse_kbbi
from aksantara.validate.schema import validate_entry


class CheckpointExecutionMixin:
    root: Path

    def _run_dir(self, run_id: str) -> Path:
        raise NotImplementedError

    def _create_run(
        self,
        preflight: CheckpointPreflight,
        run_id: str,
        idempotency_key: str,
        catalog: Mapping[str, Any],
    ) -> None:
        run_dir = self._run_dir(run_id)
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise CheckpointConflictError(
                "durable run directory already exists",
                details={"run_id": run_id},
            ) from exc
        except OSError as exc:
            raise CheckpointPersistenceError(
                "could not create durable run directory",
                details={"run_id": run_id, "error_type": type(exc).__name__},
            ) from exc
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        request = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": created_at,
            "idempotency": {
                "key": idempotency_key,
                "scope": str(self.root),
                "preimage": preflight.run_preimage,
            },
            "catalog_request": _redact_catalog_request(catalog),
            "preflight": preflight.to_dict(),
        }
        _write_json(run_dir / "request.json", request, self.root)
        _write_json(run_dir / "preflight.json", preflight.to_dict(), self.root)
        _write_state_json(
            run_dir / "status.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "created",
                "revision": 0,
                "cursor": {
                    "meaning": "number of selected keys with a committed current outcome",
                    "value": 0,
                    "limit": preflight.effective_limit,
                },
                "selected_count": len(preflight.selected),
                "outcome_counts": dict.fromkeys(_OUTCOMES, 0),
                "references": self._references(run_dir, run_id),
                "promotion": {
                    "candidate_created": False,
                    "pointer_changed": False,
                },
            },
            self.root,
        )

    def _execute(
        self,
        preflight: CheckpointPreflight,
        run_id: str,
        idempotency_key: str,
    ) -> RunResult:
        run_dir = self._run_dir(run_id)
        _write_state_json(
            run_dir / "status.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "running",
                "revision": 1,
                "cursor": {
                    "meaning": "number of selected keys with a committed current outcome",
                    "value": 0,
                    "limit": preflight.effective_limit,
                },
                "selected_count": len(preflight.selected),
                "outcome_counts": dict.fromkeys(_OUTCOMES, 0),
                "references": self._references(run_dir, run_id),
                "promotion": {
                    "candidate_created": False,
                    "pointer_changed": False,
                },
            },
            self.root,
        )
        outcomes: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        for selected_index, record in enumerate(preflight.selected):
            outcome, attempt = self._process_record(record, run_dir, selected_index)
            outcomes.append(outcome)
            attempts.append(attempt)

        outcome_counts = dict.fromkeys(_OUTCOMES, 0)
        for item in outcomes:
            current_outcome = str(item["outcome"])
            if current_outcome not in outcome_counts:
                raise CheckpointPersistenceError(
                    "unknown outcome generated by checkpoint driver",
                    details={"outcome": current_outcome},
                )
            outcome_counts[current_outcome] += 1
        current_keys = [str(item["stable_key"]) for item in outcomes]
        if len(current_keys) != len(set(current_keys)):
            raise CheckpointPersistenceError(
                "current outcome ledger contains duplicate keys"
            )
        if set(current_keys) != set(preflight.selected_keys):
            raise CheckpointPersistenceError(
                "current outcome ledger does not conserve selected keys"
            )
        has_retryable = outcome_counts["retryable"] > 0
        status = "blocked" if has_retryable else "completed"
        revision = 2
        completion_reasons: list[str] = []
        if preflight.shortfall:
            completion_reasons.append("catalog_shortfall")
        if outcome_counts["accepted"] != len(preflight.selected):
            completion_reasons.append("non_accepted_outcomes")
        if preflight.effective_limit < MAX_LIMIT:
            completion_reasons.append("effective_limit_below_fixed_100")
        if has_retryable:
            completion_reasons.append("retryable_transport_outcome")
        checkpoint_complete = (
            len(outcomes) == len(preflight.selected)
            and all(item["outcome"] in _TERMINAL_OUTCOMES for item in outcomes)
            and not has_retryable
            and not preflight.shortfall
            and preflight.effective_limit == MAX_LIMIT
        )
        # This feature deliberately stops before candidate/release work.  Even
        # a complete, all-official checkpoint is therefore not release-eligible
        # until the authority/release workers add their explicit gates.
        eligible = False
        completion_reasons.extend(
            [
                "candidate_not_created",
                "release_approval_required",
            ]
        )
        report = self._build_report(
            preflight=preflight,
            run_id=run_id,
            status=status,
            revision=revision,
            idempotency_key=idempotency_key,
            outcomes=outcomes,
            attempts=attempts,
            outcome_counts=outcome_counts,
            eligible=eligible,
            checkpoint_complete=checkpoint_complete,
            completion_reasons=completion_reasons,
            run_dir=run_dir,
        )
        _write_json(
            run_dir / "outcomes.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "snapshot": f"{run_id}:r{revision}",
                "selected_count": len(outcomes),
                "outcomes": outcomes,
            },
            self.root,
        )
        _write_json(
            run_dir / "attempts.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "attempt_count": len(attempts),
                "attempts": attempts,
            },
            self.root,
        )
        _write_json(
            run_dir / "checkpoint.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "snapshot": f"{run_id}:r{revision}",
                "cursor": {
                    "meaning": "number of selected keys with a committed current outcome",
                    "value": len(outcomes),
                    "limit": preflight.effective_limit,
                    "bounded": True,
                },
                "selected_keys": preflight.selected_keys,
                "outcome_counts": outcome_counts,
                "outcomes_reference": "outcomes.json",
                "attempts_reference": "attempts.json",
            },
            self.root,
        )
        _write_json(run_dir / "report.json", report, self.root)
        _write_state_json(
            run_dir / "status.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "status": status,
                "revision": revision,
                "cursor": {
                    "meaning": "number of selected keys with a committed current outcome",
                    "value": len(outcomes),
                    "limit": preflight.effective_limit,
                },
                "selected_count": len(preflight.selected),
                "outcome_counts": outcome_counts,
                "completion": report["completion"],
                "references": self._references(run_dir, run_id),
                "promotion": report["promotion"],
            },
            self.root,
        )
        return RunResult(
            run_id=run_id,
            status=status,
            revision=revision,
            report=report,
        )

    def _process_record(
        self,
        record: _CatalogRecord,
        run_dir: Path,
        selected_index: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        transport = record.transport
        base_attempt: dict[str, Any] = {
            "stable_key": record.stable_key,
            "selected_index": selected_index,
            "attempt": 1,
            "transport_attempt": 1,
            "validation_attempt": 0,
            "adapter": transport["adapter"],
            "status": transport["status"],
            "retry_decision": False,
            "outcome": "pending",
        }
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
                    "source_ref": record.source_identity,
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
                    "source_ref": record.source_identity,
                },
                base_attempt,
            )

        try:
            raw_bytes = self._fixture_bytes(record)
        except (CatalogValidationError, OSError) as exc:
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
                    "source_ref": record.source_identity,
                },
                base_attempt,
            )
        actual_hash = content_hash_bytes(raw_bytes)
        expected_hash = str(record.transport["expected_raw_hash"])
        if not expected_hash or actual_hash != expected_hash:
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
                    "source_ref": record.source_identity,
                },
                base_attempt,
            )
        raw_path = run_dir / "raw" / f"{actual_hash}.bin"
        _write_immutable(raw_path, raw_bytes, self.root)
        base_attempt["raw_hash"] = actual_hash
        base_attempt["validation_attempt"] = 1
        try:
            entry = parse_kbbi(raw_bytes, record.source_ref)
            validate_entry(entry, raw_bytes=raw_bytes)
            if (
                normalize_stable_key(entry.id) != record.stable_key
                and normalize_stable_key(entry.lema) != record.stable_key
            ):
                raise ValidationError("parsed entry identity does not match stable_key")
        except QuarantinedError as exc:
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
                    "raw_reference": _safe_relative(self.root, raw_path),
                    "source_ref": record.source_identity,
                },
                base_attempt,
            )
        except (ParserError, ValidationError, ValueError, TypeError) as exc:
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
                    "raw_reference": _safe_relative(self.root, raw_path),
                    "source_ref": record.source_identity,
                },
                base_attempt,
            )
        canonical_payload = entry.model_dump(mode="json")
        canonical_hash = canonical_json_hash(canonical_payload)
        parsed_path = run_dir / "parsed" / f"{record.stable_key.replace(' ', '_')}.json"
        _write_json(
            parsed_path,
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "stable_key": record.stable_key,
                "entry": canonical_payload,
                "canonical_content_hash": canonical_hash,
                "candidate_namespace": False,
            },
            self.root,
        )
        base_attempt.update(
            {
                "outcome": "accepted",
                "canonical_content_hash": canonical_hash,
            }
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
                "raw_reference": _safe_relative(self.root, raw_path),
                "parsed_reference": _safe_relative(self.root, parsed_path),
                "source_ref": record.source_identity,
                "entry_id": entry.id,
                "lema": entry.lema,
                "candidate_namespace": False,
            },
            base_attempt,
        )

    def _fixture_bytes(self, record: _CatalogRecord) -> bytes:
        transport = record.transport
        if "bytes" in transport:
            value = transport["bytes"]
            if isinstance(value, bytes):
                return value
            else:
                raise CatalogValidationError("fixture inline bytes are malformed")
        path_value = transport.get("path")
        if not isinstance(path_value, str):
            raise CatalogValidationError("fixture binding has no readable path")
        path = (self.root / Path(path_value)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise CatalogValidationError(
                "fixture path escapes caller root",
                details={"path": path_value},
            ) from exc
        return path.read_bytes()

    def _references(self, run_dir: Path, run_id: str) -> dict[str, str]:
        return {
            "run": _safe_relative(self.root, run_dir),
            "status": _safe_relative(self.root, run_dir / "status.json"),
            "report": _safe_relative(self.root, run_dir / "report.json"),
            "checkpoint": _safe_relative(self.root, run_dir / "checkpoint.json"),
            "outcomes": _safe_relative(self.root, run_dir / "outcomes.json"),
            "attempts": _safe_relative(self.root, run_dir / "attempts.json"),
            "id": run_id,
        }

    def _build_report(
        self,
        *,
        preflight: CheckpointPreflight,
        run_id: str,
        status: str,
        revision: int,
        idempotency_key: str,
        outcomes: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        outcome_counts: dict[str, int],
        eligible: bool,
        completion_reasons: list[str],
        checkpoint_complete: bool,
        run_dir: Path,
    ) -> dict[str, Any]:
        exclusions = [
            {
                "stable_key": item["stable_key"],
                "reason": item.get(
                    "exclusion_reason", item.get("reason", "not_eligible")
                ),
                "outcome": item["outcome"],
            }
            for item in outcomes
            if item["outcome"] != "accepted"
        ]
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": run_id,
            "status": status,
            "revision": revision,
            "snapshot": f"{run_id}:r{revision}",
            "corpus": {
                "catalog_id": preflight.catalog_id,
                "corpus_version": preflight.corpus_version,
                "catalog_count": len(preflight.records),
            },
            "selection": {
                "algorithm": preflight.selection_algorithm,
                "requested_limit": preflight.requested_limit,
                "effective_limit": preflight.effective_limit,
                "selected_count": len(preflight.selected),
                "shortfall": preflight.shortfall,
                "selected_keys": preflight.selected_keys,
            },
            "selected_keys": preflight.selected_keys,
            "selected_count": len(preflight.selected),
            "current_outcome_count": len(outcomes),
            "outcome_counts": outcome_counts,
            "outcomes": outcomes,
            "attempt_count": len(attempts),
            "exclusions": exclusions,
            "completion": {
                "state": "blocked" if status == "blocked" else "completed",
                "terminal": True,
                "checkpoint_complete": checkpoint_complete,
                "eligible": eligible,
                "reasons": completion_reasons,
            },
            "eligibility": {
                "eligible": eligible,
                "checkpoint_complete": checkpoint_complete,
                "candidate_created": False,
                "candidate_count": 0,
                "reason_codes": completion_reasons,
                "accepted_keys": [
                    item["stable_key"]
                    for item in outcomes
                    if item["outcome"] == "accepted"
                ],
                "excluded_keys": [item["stable_key"] for item in exclusions],
                "requires_release_approval": True,
            },
            "fingerprints": {
                "catalog": preflight.catalog_fingerprint,
                "run": preflight.run_fingerprint,
                "preimages": {
                    "catalog": preflight.catalog_preimage,
                    "run": preflight.run_preimage,
                },
            },
            "authority": {
                "mode": preflight.authority_policy,
                "policy_version": preflight.validation_policy,
                "canonical_writes": False,
                "source_kinds_are_verified_by_adapter": True,
            },
            "comparison": {
                "policy": preflight.comparison_policy,
                "actual_bytes_hashed_before_parse": True,
            },
            "pins": {
                "parser_version": preflight.parser_version,
                "transform_version": preflight.transform_version,
                "validation_policy": preflight.validation_policy,
                "selection_algorithm": preflight.selection_algorithm,
            },
            "idempotency": {
                "key": idempotency_key,
                "scope": str(self.root),
                "tuple": preflight.run_preimage,
            },
            "roots": {
                "caller_root": str(self.root),
                "run": _safe_relative(self.root, run_dir),
                "all_artifacts_under_caller_root": True,
            },
            "references": self._references(run_dir, run_id),
            "network_trace": {
                "mode": "local-fixture-only",
                "live_network_attempts": 0,
                "gcp_attempts": 0,
                "emulator_attempts": 0,
                "unapproved_host_attempts": 0,
                "source_reads": sum(
                    1 for attempt in attempts if int(attempt.get("status", 0)) < 400
                ),
                "unselected_reads": 0,
                "source_order": preflight.selected_keys,
            },
            "promotion": {
                "candidate_created": False,
                "pointer_changed": False,
                "current_version_before": None,
                "current_version_after": None,
                "release_operation": "not_invoked",
            },
            "conservation": {
                "selected_count": len(preflight.selected),
                "unique_current_outcome_keys": len(
                    {item["stable_key"] for item in outcomes}
                ),
                "sum_outcome_counts": sum(outcome_counts.values()),
                "attempt_count_separate": len(attempts),
                "partition_holds": (
                    len(preflight.selected)
                    == len({item["stable_key"] for item in outcomes})
                    == sum(outcome_counts.values())
                ),
            },
        }
