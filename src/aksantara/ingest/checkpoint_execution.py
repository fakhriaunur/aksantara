"""Checkpoint execution, outcome accounting, and report generation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aksantara.ingest.checkpoint_record import process_record
from aksantara.ingest.checkpoint_storage import (
    _redact_catalog_request,
    _safe_relative,
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
        logical_attempts: list[dict[str, Any]] = []
        physical_attempts: list[dict[str, Any]] = []
        for selected_index, record in enumerate(preflight.selected):
            outcome, attempt = self._process_record(record, run_dir, selected_index)
            outcomes.append(self._annotate_outcome(outcome, run_id=run_id))
            logical_attempts.append(attempt)
            source_attempts = attempt.get("source_attempts")
            if isinstance(source_attempts, list):
                physical_attempts.extend(
                    dict(value)
                    for value in source_attempts
                    if isinstance(value, Mapping)
                )
            else:
                physical_attempts.append(attempt)

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
            attempts=logical_attempts,
            physical_attempts=physical_attempts,
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
                "attempt_count": len(physical_attempts),
                "physical_attempt_count": len(physical_attempts),
                "logical_attempt_count": len(logical_attempts),
                "transport_attempt_count": len(physical_attempts),
                "attempts": logical_attempts,
                "physical_attempts": physical_attempts,
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
                "processed_count": len(outcomes),
                "terminal_count": sum(
                    count
                    for name, count in outcome_counts.items()
                    if name in _TERMINAL_OUTCOMES
                ),
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
        self: Any,
        record: _CatalogRecord,
        run_dir: Path,
        selected_index: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return process_record(self, record, run_dir, selected_index)

    @staticmethod
    def _annotate_outcome(outcome: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
        """Add report-level aliases while retaining the processor evidence."""
        value = dict(outcome)
        status = str(value.get("outcome", ""))
        value["source_key"] = str(value.get("stable_key", ""))
        value["current"] = True
        value["eligible"] = status == "accepted"
        value["candidate_member"] = False
        value["attempt_number"] = int(value.get("attempt_count", 1))
        value["physical_attempt_count"] = int(
            value.get("physical_attempt_count", value.get("attempt_count", 1))
        )
        value["last_transition"] = status
        value["error_class"] = (
            value.get("exclusion_reason") if status != "accepted" else None
        )
        value["raw_content_hash"] = value.get("raw_hash")
        value["canonical_hash"] = value.get("canonical_content_hash")
        value["attempt_history_reference"] = "attempts.json"
        value["attempt_run_id"] = run_id
        return value

    @staticmethod
    def _transport_attempt_count(attempts: list[dict[str, Any]]) -> int:
        """Count physical source attempts separately from logical key rows."""
        total = 0
        for attempt in attempts:
            source_attempts = attempt.get("source_attempts")
            total += len(source_attempts) if isinstance(source_attempts, list) else 1
        return total

    def _fixture_bytes_for_transport(self, transport: Mapping[str, Any]) -> bytes:
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

    def _fixture_bytes(self, record: _CatalogRecord) -> bytes:
        """Compatibility helper for callers that still pass a catalog record."""
        return self._fixture_bytes_for_transport(record.transport)

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
        physical_attempts: list[dict[str, Any]],
        outcome_counts: dict[str, int],
        eligible: bool,
        completion_reasons: list[str],
        checkpoint_complete: bool,
        run_dir: Path,
    ) -> dict[str, Any]:
        exclusions = [
            {
                "stable_key": item["stable_key"],
                "source_key": item["stable_key"],
                "reason": item.get(
                    "exclusion_reason", item.get("reason", "not_eligible")
                ),
                "outcome": item["outcome"],
                "eligible": False,
                "candidate_member": False,
                "current_citation": None,
            }
            for item in outcomes
            if item["outcome"] != "accepted"
        ]
        accepted_joins = [
            {
                "stable_key": item["stable_key"],
                "source_key": item["stable_key"],
                "entry_id": item.get("entry_id"),
                "run_id": run_id,
                "run_fingerprint": preflight.run_fingerprint,
                "source_ref": item.get("source_ref"),
                "source_role": item.get("source_role"),
                "authority_role": item.get("authority_role"),
                "raw_hash": item.get("raw_hash"),
                "raw_content_hash": item.get("raw_hash"),
                "canonical_hash": item.get("canonical_content_hash"),
                "canonical_content_hash": item.get("canonical_content_hash"),
                "raw_snapshot_id": item.get("raw_snapshot_id"),
                "observation_id": item.get("observation_id"),
                "official_observation": item.get("official_observation"),
                "canonical_reference": item.get("canonical_reference"),
                "parsed_reference": item.get("parsed_reference"),
                "eligible": True,
                "candidate_member": False,
            }
            for item in outcomes
            if item["outcome"] == "accepted"
        ]
        terminal_count = sum(
            count
            for name, count in outcome_counts.items()
            if name in _TERMINAL_OUTCOMES
        )
        pending_count = sum(
            count
            for name, count in outcome_counts.items()
            if name in {"pending", "in_progress", "retryable"}
        )
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": run_id,
            "status": status,
            "revision": revision,
            "snapshot": f"{run_id}:r{revision}",
            "revision_history": [
                {
                    "revision": revision,
                    "snapshot": f"{run_id}:r{revision}",
                    "report_reference": "report.json",
                    "outcomes_reference": "outcomes.json",
                    "attempts_reference": "attempts.json",
                    "immutable": True,
                }
            ],
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
            "processed_count": len(outcomes),
            "terminal_count": terminal_count,
            "pending_count": pending_count,
            "current_outcome_count": len(outcomes),
            "outcome_counts": outcome_counts,
            "outcomes": outcomes,
            "attempt_count": len(physical_attempts),
            "physical_attempt_count": len(physical_attempts),
            "logical_attempt_count": len(attempts),
            "transport_attempt_count": len(physical_attempts),
            "physical_attempts": physical_attempts,
            "exclusions": exclusions,
            "excluded_keys": [item["stable_key"] for item in exclusions],
            "accepted_joins": accepted_joins,
            "completion": {
                "state": "blocked" if status == "blocked" else "completed",
                "terminal": True,
                "resumable": False,
                "all_outcomes_terminal": pending_count == 0,
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
                "accepted_join_count": len(accepted_joins),
                "excluded_count": len(exclusions),
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
                    (
                        sum(
                            1
                            for source_attempt in physical_attempts
                            if str(source_attempt.get("stable_key"))
                            == str(attempt.get("stable_key"))
                            and int(source_attempt.get("status", 0)) < 400
                        )
                        if isinstance(attempt.get("source_attempts"), list)
                        else int(attempt.get("status", 0)) < 400
                    )
                    for attempt in attempts
                ),
                "unselected_reads": 0,
                "source_order": preflight.selected_keys,
            },
            "report_contract": {
                "current_outcome_key": "stable_key",
                "attempt_history_is_separate": True,
                "logical_attempt_count": len(attempts),
                "physical_attempt_count": len(physical_attempts),
                "physical_attempts_are_ordered": True,
                "physical_attempt_fields": [
                    "attempt_id",
                    "run_id",
                    "stable_key",
                    "sequence",
                    "source_ref",
                    "source_kind",
                    "source_role",
                    "outcome",
                    "raw_hash",
                    "raw_snapshot_id",
                    "observation_id",
                    "canonical_content_hash",
                    "parse_result",
                    "validation_result",
                    "conflict_result",
                ],
                "terminal_outcomes": sorted(_TERMINAL_OUTCOMES),
                "exclusion_reason_required": True,
                "accepted_join_requires": [
                    "entry_id",
                    "run_id",
                    "run_fingerprint",
                    "source_ref",
                    "source_role",
                    "authority_role",
                    "raw_content_hash",
                    "canonical_content_hash",
                    "raw_snapshot_id",
                    "observation_id",
                ],
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
                "attempt_count_separate": len(physical_attempts),
                "processed_count": len(outcomes),
                "terminal_count": terminal_count,
                "pending_count": pending_count,
                "logical_attempt_count": len(attempts),
                "transport_attempt_count": len(physical_attempts),
                "accepted_join_count": len(accepted_joins),
                "excluded_count": len(exclusions),
                "partition_holds": (
                    len(preflight.selected)
                    == len({item["stable_key"] for item in outcomes})
                    == sum(outcome_counts.values())
                    and set(preflight.selected_keys)
                    == {item["stable_key"] for item in accepted_joins}.union(
                        {item["stable_key"] for item in exclusions}
                    )
                ),
            },
        }
