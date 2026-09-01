"""Checkpoint execution, outcome accounting, and report generation with resume, lease, and barrier support."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aksantara.ingest.checkpoint_record import process_record
from aksantara.ingest.checkpoint_storage import (
    _check_fence,
    _read_json,
    _read_lease,
    _redact_catalog_request,
    _safe_relative,
    _write_json,
    _write_lease,
    _write_state_json,
)
from aksantara.ingest.checkpoint_types import (
    _BARRIER_PHASES,
    _LEASE_HEARTBEAT_SECONDS,
    _LEASE_TTL_SECONDS,
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
        # Lease for fencing: generation 1, owner is current process.
        lease = {
            "schema_version": "checkpoint-lease-v1",
            "run_id": run_id,
            "owner": f"pid:{os.getpid()}",
            "owner_pid": os.getpid(),
            "operation": "run",
            "generation": 1,
            "fence_token": str(uuid.uuid4()),
            "created_at": created_at,
            "heartbeat": created_at,
            "expiry": (datetime.now(UTC) + timedelta(seconds=_LEASE_TTL_SECONDS))
            .isoformat()
            .replace("+00:00", "Z"),
            "ttl_seconds": _LEASE_TTL_SECONDS,
            "heartbeat_seconds": _LEASE_HEARTBEAT_SECONDS,
            "state": "held",
        }
        _write_lease(run_dir, lease, self.root)
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
                    "bounded": True,
                    "monotonic": True,
                },
                "selected_count": len(preflight.selected),
                "outcome_counts": dict.fromkeys(_OUTCOMES, 0),
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "references": self._references(run_dir, run_id),
                "promotion": {
                    "candidate_created": False,
                    "pointer_changed": False,
                },
                "lease": self._lease_diagnostics(run_dir),
                "barrier": None,
                "idempotency": {
                    "key": idempotency_key,
                    "scope": str(self.root),
                },
                "totals": {
                    "selected": len(preflight.selected),
                    "processed": 0,
                    "terminal": 0,
                    "pending": len(preflight.selected),
                },
            },
            self.root,
        )

    def _lease_diagnostics(self, run_dir: Path) -> dict[str, Any]:
        lease = _read_lease(run_dir)
        if lease is None:
            return {
                "owner": None,
                "operation": None,
                "generation": 0,
                "fence_token": None,
                "expiry": None,
                "heartbeat": None,
                "reclaimable": True,
            }
        return {
            "owner": lease.get("owner"),
            "owner_pid": lease.get("owner_pid"),
            "operation": lease.get("operation"),
            "generation": lease.get("generation"),
            "fence_token": lease.get("fence_token"),
            "expiry": lease.get("expiry"),
            "heartbeat": lease.get("heartbeat"),
            "ttl_seconds": lease.get("ttl_seconds"),
            "state": lease.get("state"),
            "reclaimable": True,
        }

    def _execute(
        self,
        preflight: CheckpointPreflight,
        run_id: str,
        idempotency_key: str,
        *,
        barrier_phase: str | None = None,
        barrier_hold_seconds: float = 0,
        interrupt_after: int | None = None,
        expected_generation: int | None = None,
    ) -> RunResult:
        """Incrementally execute with durable checkpoint-per-cursor and lease fencing."""
        run_dir = self._run_dir(run_id)
        # Validate barrier phase
        if barrier_phase is not None and barrier_phase not in _BARRIER_PHASES:
            raise CatalogValidationError(
                "barrier phase must be one of the published phases",
                details={"phase": barrier_phase, "allowed": list(_BARRIER_PHASES)},
            )
        # Determine expected generation
        lease = _read_lease(run_dir)
        gen = int(lease.get("generation", 1)) if lease else 1
        if expected_generation is not None:
            gen = expected_generation
            _check_fence(run_dir, gen)
        # Prepare to run from cursor
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
                    "bounded": True,
                    "monotonic": True,
                },
                "selected_count": len(preflight.selected),
                "outcome_counts": dict.fromkeys(_OUTCOMES, 0),
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "references": self._references(run_dir, run_id),
                "promotion": {
                    "candidate_created": False,
                    "pointer_changed": False,
                },
                "lease": self._lease_diagnostics(run_dir),
                "barrier": None,
                "idempotency": {
                    "key": idempotency_key,
                    "scope": str(self.root),
                },
                "totals": {
                    "selected": len(preflight.selected),
                    "processed": 0,
                    "terminal": 0,
                    "pending": len(preflight.selected),
                },
            },
            self.root,
        )
        # Heartbeat
        self._heartbeat_lease(run_dir, gen)
        # Incremental execution
        outcomes: list[dict[str, Any]] = []
        logical_attempts: list[dict[str, Any]] = []
        physical_attempts: list[dict[str, Any]] = []
        # For combined-transaction barrier, we hold after each checkpoint commit.
        # For checkpoint-before-cursor, we split checkpoint and cursor writes.
        revision = 1
        for selected_index, record in enumerate(preflight.selected):
            # Check fence before processing
            _check_fence(run_dir, gen)
            # Barrier before-write
            if barrier_phase == "before-write" and barrier_hold_seconds > 0:
                self._barrier_hold(
                    run_dir,
                    run_id,
                    barrier_phase,
                    selected_index,
                    barrier_hold_seconds,
                    gen,
                )
                _check_fence(run_dir, gen)
            outcome, attempt = self._process_record(record, run_dir, selected_index)
            annotated_outcome = self._annotate_outcome(
                outcome,
                run_id=run_id,
                catalog_fingerprint=preflight.catalog_fingerprint,
                run_fingerprint=preflight.run_fingerprint,
                pins={
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
            )
            outcomes.append(annotated_outcome)
            logical_attempts.append(attempt)
            attempt["run_fingerprint"] = preflight.run_fingerprint
            attempt["pins"] = {
                "parser_version": preflight.parser_version,
                "transform_version": preflight.transform_version,
                "validation_policy": preflight.validation_policy,
            }
            source_attempts = attempt.get("source_attempts")
            if isinstance(source_attempts, list):
                annotated_source_attempts = [
                    self._annotate_physical_attempt(
                        value,
                        catalog_fingerprint=preflight.catalog_fingerprint,
                        run_fingerprint=preflight.run_fingerprint,
                        pins={
                            "parser_version": preflight.parser_version,
                            "transform_version": preflight.transform_version,
                            "validation_policy": preflight.validation_policy,
                        },
                    )
                    for value in source_attempts
                    if isinstance(value, Mapping)
                ]
                attempt["source_attempts"] = annotated_source_attempts
                physical_attempts.extend(annotated_source_attempts)
            else:
                physical_attempts.append(
                    self._annotate_physical_attempt(
                        attempt,
                        catalog_fingerprint=preflight.catalog_fingerprint,
                        run_fingerprint=preflight.run_fingerprint,
                        pins={
                            "parser_version": preflight.parser_version,
                            "transform_version": preflight.transform_version,
                            "validation_policy": preflight.validation_policy,
                        },
                    )
                )
            # Barrier durable-write-before-ack
            if barrier_phase == "durable-write-before-ack" and barrier_hold_seconds > 0:
                self._barrier_hold(
                    run_dir,
                    run_id,
                    barrier_phase,
                    selected_index,
                    barrier_hold_seconds,
                    gen,
                )
                _check_fence(run_dir, gen)
            # Commit this revision
            revision = (
                2 + selected_index
            )  # revision 2 is first key, etc. Simpler: incremental
            # For checkpoint-before-cursor, split writes: first checkpoint, hold, then status
            if barrier_phase == "checkpoint-before-cursor" and barrier_hold_seconds > 0:
                # First commit checkpoint/outcomes/attempts without cursor advance in status
                self._commit_checkpoint_only(
                    preflight=preflight,
                    run_id=run_id,
                    revision=revision,
                    outcomes=outcomes,
                    logical_attempts=logical_attempts,
                    physical_attempts=physical_attempts,
                    gen=gen,
                )
                self._barrier_hold(
                    run_dir,
                    run_id,
                    barrier_phase,
                    selected_index,
                    barrier_hold_seconds,
                    gen,
                )
                _check_fence(run_dir, gen)
                self._update_status_after_checkpoint(
                    preflight=preflight,
                    run_id=run_id,
                    revision=revision,
                    outcomes=outcomes,
                    physical_attempts=physical_attempts,
                    idempotency_key=idempotency_key,
                    gen=gen,
                )
            else:
                # Combined transaction: checkpoint + cursor together
                if barrier_phase == "combined-transaction" and barrier_hold_seconds > 0:
                    self._barrier_hold(
                        run_dir,
                        run_id,
                        barrier_phase,
                        selected_index,
                        barrier_hold_seconds,
                        gen,
                    )
                    _check_fence(run_dir, gen)
                self._commit_revision(
                    preflight=preflight,
                    run_id=run_id,
                    revision=revision,
                    outcomes=outcomes,
                    logical_attempts=logical_attempts,
                    physical_attempts=physical_attempts,
                    idempotency_key=idempotency_key,
                    gen=gen,
                )
            self._heartbeat_lease(run_dir, gen)
            # Check interrupt after committing this key
            if interrupt_after is not None and len(outcomes) >= interrupt_after:
                # Mark interrupted, not completed
                self._mark_interrupted(
                    preflight=preflight,
                    run_id=run_id,
                    revision=revision,
                    outcomes=outcomes,
                    physical_attempts=physical_attempts,
                    idempotency_key=idempotency_key,
                    gen=gen,
                )
                # Return interrupted result
                report = _read_json(run_dir / "report.json")
                return RunResult(
                    run_id=run_id,
                    status="interrupted",
                    revision=revision,
                    report=report,
                )
        # Finalize as completed/blocked if not interrupted
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
        # Final report already committed in last revision, just update status if needed
        # Ensure final status is correct (if last commit already had it, keep)
        final_status = _read_json(run_dir / "status.json")
        if final_status.get("status") != status:
            # Update to final terminal state
            _write_state_json(
                run_dir / "status.json",
                {
                    **final_status,
                    "status": status,
                    "completion": _read_json(run_dir / "report.json").get(
                        "completion", {}
                    ),
                    "lease": self._lease_diagnostics(run_dir),
                },
                self.root,
            )
        report = _read_json(run_dir / "report.json")
        return RunResult(
            run_id=run_id,
            status=status,
            revision=revision,
            report=report,
        )

    def _commit_revision(
        self,
        *,
        preflight: CheckpointPreflight,
        run_id: str,
        revision: int,
        outcomes: list[dict[str, Any]],
        logical_attempts: list[dict[str, Any]],
        physical_attempts: list[dict[str, Any]],
        idempotency_key: str,
        gen: int,
    ) -> None:
        run_dir = self._run_dir(run_id)
        _check_fence(run_dir, gen)
        outcome_counts = dict.fromkeys(_OUTCOMES, 0)
        for item in outcomes:
            outcome_counts[str(item["outcome"])] += 1
        has_retryable = outcome_counts["retryable"] > 0
        # Interim status: running unless this is final and has retryable -> blocked, or completed
        is_final = len(outcomes) == len(preflight.selected)
        if is_final:
            status = "blocked" if has_retryable else "completed"
        else:
            status = "running"
        # Build report for this revision
        completion_reasons: list[str] = []
        if preflight.shortfall:
            completion_reasons.append("catalog_shortfall")
        if len(outcomes) < len(preflight.selected):
            completion_reasons.append("incomplete_checkpoint")
        elif outcome_counts["accepted"] != len(preflight.selected):
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
        eligible = False
        if is_final:
            completion_reasons.extend(
                ["candidate_not_created", "release_approval_required"]
            )
        report = self._build_report(
            preflight=preflight,
            run_id=run_id,
            status=status,
            revision=revision,
            idempotency_key=idempotency_key,
            outcomes=list(outcomes),
            attempts=list(logical_attempts),
            physical_attempts=list(physical_attempts),
            outcome_counts=dict(outcome_counts),
            eligible=eligible,
            checkpoint_complete=checkpoint_complete,
            completion_reasons=list(completion_reasons),
            run_dir=run_dir,
        )
        # Add lease and barrier diagnostics to report
        report["lease"] = self._lease_diagnostics(run_dir)
        report["barrier"] = None
        report["cursor"] = {
            "meaning": "number of selected keys with a committed current outcome",
            "value": len(outcomes),
            "limit": preflight.effective_limit,
            "bounded": True,
            "monotonic": True,
        }
        report["window"] = {
            "model": "serial",
            "in_flight": [],
            "committed": len(outcomes),
            "total": len(preflight.selected),
        }
        _write_state_json(
            run_dir / "outcomes.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "snapshot": f"{run_id}:r{revision}",
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "selected_count": len(outcomes),
                "outcomes": list(outcomes),
                "cursor": report["cursor"],
            },
            self.root,
        )
        _write_state_json(
            run_dir / "attempts.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "attempt_count": len(physical_attempts),
                "physical_attempt_count": len(physical_attempts),
                "logical_attempt_count": len(logical_attempts),
                "transport_attempt_count": len(physical_attempts),
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "attempts": list(logical_attempts),
                "physical_attempts": list(physical_attempts),
            },
            self.root,
        )
        _write_state_json(
            run_dir / "checkpoint.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "snapshot": f"{run_id}:r{revision}",
                "cursor": report["cursor"],
                "window": report["window"],
                "selected_keys": preflight.selected_keys,
                "outcome_counts": dict(outcome_counts),
                "processed_count": len(outcomes),
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "terminal_count": sum(
                    count
                    for name, count in outcome_counts.items()
                    if name in _TERMINAL_OUTCOMES
                ),
                "outcomes_reference": "outcomes.json",
                "attempts_reference": "attempts.json",
                "lease": self._lease_diagnostics(run_dir),
                "idempotency": {
                    "key": idempotency_key,
                    "scope": str(self.root),
                },
            },
            self.root,
        )
        _write_state_json(run_dir / "report.json", report, self.root)
        _write_state_json(
            run_dir / "status.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "status": status,
                "revision": revision,
                "snapshot": f"{run_id}:r{revision}",
                "cursor": report["cursor"],
                "window": report["window"],
                "selected_count": len(preflight.selected),
                "outcome_counts": dict(outcome_counts),
                "completion": report["completion"],
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "references": self._references(run_dir, run_id),
                "promotion": report["promotion"],
                "lease": self._lease_diagnostics(run_dir),
                "barrier": None,
                "idempotency": {
                    "key": idempotency_key,
                    "scope": str(self.root),
                },
                "totals": {
                    "selected": len(preflight.selected),
                    "processed": len(outcomes),
                    "terminal": sum(
                        count
                        for name, count in outcome_counts.items()
                        if name in _TERMINAL_OUTCOMES
                    ),
                    "pending": len(preflight.selected) - len(outcomes),
                },
            },
            self.root,
        )

    def _commit_checkpoint_only(
        self,
        *,
        preflight: CheckpointPreflight,
        run_id: str,
        revision: int,
        outcomes: list[dict[str, Any]],
        logical_attempts: list[dict[str, Any]],
        physical_attempts: list[dict[str, Any]],
        gen: int,
    ) -> None:
        run_dir = self._run_dir(run_id)
        _check_fence(run_dir, gen)
        outcome_counts = dict.fromkeys(_OUTCOMES, 0)
        for item in outcomes:
            outcome_counts[str(item["outcome"])] += 1
        _write_state_json(
            run_dir / "outcomes.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "snapshot": f"{run_id}:r{revision}",
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "selected_count": len(outcomes),
                "outcomes": list(outcomes),
                "cursor": {
                    "meaning": "number of selected keys with a committed current outcome",
                    "value": len(outcomes),
                    "limit": preflight.effective_limit,
                },
            },
            self.root,
        )
        _write_state_json(
            run_dir / "attempts.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "revision": revision,
                "attempt_count": len(physical_attempts),
                "physical_attempt_count": len(physical_attempts),
                "logical_attempt_count": len(logical_attempts),
                "transport_attempt_count": len(physical_attempts),
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "attempts": list(logical_attempts),
                "physical_attempts": list(physical_attempts),
            },
            self.root,
        )
        _write_state_json(
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
                "outcome_counts": dict(outcome_counts),
                "processed_count": len(outcomes),
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "terminal_count": sum(
                    count
                    for name, count in outcome_counts.items()
                    if name in _TERMINAL_OUTCOMES
                ),
                "outcomes_reference": "outcomes.json",
                "attempts_reference": "attempts.json",
                "lease": self._lease_diagnostics(run_dir),
            },
            self.root,
        )

    def _update_status_after_checkpoint(
        self,
        *,
        preflight: CheckpointPreflight,
        run_id: str,
        revision: int,
        outcomes: list[dict[str, Any]],
        physical_attempts: list[dict[str, Any]],
        idempotency_key: str,
        gen: int,
    ) -> None:
        run_dir = self._run_dir(run_id)
        _check_fence(run_dir, gen)
        outcome_counts = dict.fromkeys(_OUTCOMES, 0)
        for item in outcomes:
            outcome_counts[str(item["outcome"])] += 1
        has_retryable = outcome_counts["retryable"] > 0
        is_final = len(outcomes) == len(preflight.selected)
        if is_final:
            status = "blocked" if has_retryable else "completed"
        else:
            status = "running"
        completion_reasons: list[str] = []
        if preflight.shortfall:
            completion_reasons.append("catalog_shortfall")
        if len(outcomes) < len(preflight.selected):
            completion_reasons.append("incomplete_checkpoint")
        elif outcome_counts["accepted"] != len(preflight.selected):
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
        eligible = False
        if is_final:
            completion_reasons.extend(
                ["candidate_not_created", "release_approval_required"]
            )
        report = self._build_report(
            preflight=preflight,
            run_id=run_id,
            status=status,
            revision=revision,
            idempotency_key=idempotency_key,
            outcomes=list(outcomes),
            attempts=[],  # will be filled from existing? simplified
            physical_attempts=list(physical_attempts),
            outcome_counts=dict(outcome_counts),
            eligible=eligible,
            checkpoint_complete=checkpoint_complete,
            completion_reasons=list(completion_reasons),
            run_dir=run_dir,
        )
        # Use existing logical attempts if available
        try:
            existing_attempts = _read_json(run_dir / "attempts.json").get(
                "attempts", []
            )
            report["attempts"] = existing_attempts
        except Exception:
            pass
        report["lease"] = self._lease_diagnostics(run_dir)
        report["cursor"] = {
            "meaning": "number of selected keys with a committed current outcome",
            "value": len(outcomes),
            "limit": preflight.effective_limit,
            "bounded": True,
            "monotonic": True,
        }
        _write_state_json(run_dir / "report.json", report, self.root)
        _write_state_json(
            run_dir / "status.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": run_id,
                "status": status,
                "revision": revision,
                "snapshot": f"{run_id}:r{revision}",
                "cursor": report["cursor"],
                "selected_count": len(preflight.selected),
                "outcome_counts": dict(outcome_counts),
                "completion": report["completion"],
                "fingerprints": {
                    "catalog": preflight.catalog_fingerprint,
                    "run": preflight.run_fingerprint,
                },
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "references": self._references(run_dir, run_id),
                "promotion": report["promotion"],
                "lease": self._lease_diagnostics(run_dir),
                "barrier": None,
                "idempotency": {
                    "key": idempotency_key,
                    "scope": str(self.root),
                },
                "totals": {
                    "selected": len(preflight.selected),
                    "processed": len(outcomes),
                    "terminal": sum(
                        count
                        for name, count in outcome_counts.items()
                        if name in _TERMINAL_OUTCOMES
                    ),
                    "pending": len(preflight.selected) - len(outcomes),
                },
            },
            self.root,
        )

    def _mark_interrupted(
        self,
        *,
        preflight: CheckpointPreflight,
        run_id: str,
        revision: int,
        outcomes: list[dict[str, Any]],
        physical_attempts: list[dict[str, Any]],
        idempotency_key: str,
        gen: int,
    ) -> None:
        run_dir = self._run_dir(run_id)
        # Already committed this revision, now mark status as interrupted
        outcome_counts = dict.fromkeys(_OUTCOMES, 0)
        for item in outcomes:
            outcome_counts[str(item["outcome"])] += 1
        # Update lease to reflect interrupted
        lease = _read_lease(run_dir)
        if lease:
            lease["state"] = "interrupted"
            lease["heartbeat"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            _write_lease(run_dir, lease, self.root)
        status_payload = _read_json(run_dir / "status.json")
        status_payload["status"] = "interrupted"
        status_payload["lease"] = self._lease_diagnostics(run_dir)
        status_payload["resumable"] = True
        status_payload["completion"] = {
            "state": "interrupted",
            "terminal": False,
            "resumable": True,
            "all_outcomes_terminal": False,
            "checkpoint_complete": False,
            "eligible": False,
            "reasons": ["interrupted"],
        }
        # Report also interrupted
        try:
            report = _read_json(run_dir / "report.json")
            report["status"] = "interrupted"
            report["completion"] = status_payload["completion"]
            report["lease"] = status_payload["lease"]
            _write_state_json(run_dir / "report.json", report, self.root)
        except Exception:
            pass
        _write_state_json(run_dir / "status.json", status_payload, self.root)

    def _heartbeat_lease(self, run_dir: Path, generation: int) -> None:
        lease = _read_lease(run_dir)
        if lease is None:
            return
        if int(lease.get("generation", 0)) != generation:
            return
        lease["heartbeat"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        lease["expiry"] = (
            (datetime.now(UTC) + timedelta(seconds=_LEASE_TTL_SECONDS))
            .isoformat()
            .replace("+00:00", "Z")
        )
        try:
            _write_lease(run_dir, lease, self.root)
        except Exception:
            pass

    def _barrier_hold(
        self,
        run_dir: Path,
        run_id: str,
        phase: str,
        selected_index: int,
        hold_seconds: float,
        generation: int,
    ) -> None:
        barrier_id = f"{run_id}:{phase}:{selected_index}:{uuid.uuid4().hex[:8]}"
        barrier_payload = {
            "schema_version": "checkpoint-barrier-v1",
            "barrier_id": barrier_id,
            "run_id": run_id,
            "phase": phase,
            "selected_index": selected_index,
            "generation": generation,
            "fence_token": (_read_lease(run_dir) or {}).get("fence_token"),
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "hold_seconds": hold_seconds,
            "owner": f"pid:{os.getpid()}",
            "lease": self._lease_diagnostics(run_dir),
        }
        barrier_path = run_dir / f"barrier-{phase}-{selected_index}.json"
        # Write barrier file (caller-owned, process-scoped)
        try:
            _write_state_json(barrier_path, barrier_payload, self.root)
            # Also update status to show barrier
            try:
                status = _read_json(run_dir / "status.json")
                status["barrier"] = barrier_payload
                status["lease"] = self._lease_diagnostics(run_dir)
                _write_state_json(run_dir / "status.json", status, self.root)
            except Exception:
                pass
            time.sleep(hold_seconds)
        finally:
            # Clear barrier but keep history in status as last barrier?
            try:
                barrier_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                status = _read_json(run_dir / "status.json")
                status["barrier"] = None
                _write_state_json(run_dir / "status.json", status, self.root)
            except Exception:
                pass

    def _resume_incremental(
        self,
        preflight: CheckpointPreflight,
        run_id: str,
        idempotency_key: str,
        existing_outcomes: list[dict[str, Any]],
        existing_logical: list[dict[str, Any]],
        existing_physical: list[dict[str, Any]],
        expected_generation: int,
    ) -> RunResult:
        run_dir = self._run_dir(run_id)
        _check_fence(run_dir, expected_generation)
        # Verify we are resuming from correct cursor
        cursor = len(existing_outcomes)
        outcomes = list(existing_outcomes)
        logical_attempts = list(existing_logical)
        physical_attempts = list(existing_physical)
        revision_start = 2 + cursor  # next revision
        # Reclaim lease already done before calling
        for selected_index in range(cursor, len(preflight.selected)):
            record = preflight.selected[selected_index]
            _check_fence(run_dir, expected_generation)
            outcome, attempt = self._process_record(record, run_dir, selected_index)
            annotated_outcome = self._annotate_outcome(
                outcome,
                run_id=run_id,
                catalog_fingerprint=preflight.catalog_fingerprint,
                run_fingerprint=preflight.run_fingerprint,
                pins={
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
            )
            outcomes.append(annotated_outcome)
            logical_attempts.append(attempt)
            attempt["run_fingerprint"] = preflight.run_fingerprint
            attempt["pins"] = {
                "parser_version": preflight.parser_version,
                "transform_version": preflight.transform_version,
                "validation_policy": preflight.validation_policy,
            }
            source_attempts = attempt.get("source_attempts")
            if isinstance(source_attempts, list):
                annotated_source_attempts = [
                    self._annotate_physical_attempt(
                        value,
                        catalog_fingerprint=preflight.catalog_fingerprint,
                        run_fingerprint=preflight.run_fingerprint,
                        pins={
                            "parser_version": preflight.parser_version,
                            "transform_version": preflight.transform_version,
                            "validation_policy": preflight.validation_policy,
                        },
                    )
                    for value in source_attempts
                    if isinstance(value, Mapping)
                ]
                attempt["source_attempts"] = annotated_source_attempts
                physical_attempts.extend(annotated_source_attempts)
            else:
                physical_attempts.append(
                    self._annotate_physical_attempt(
                        attempt,
                        catalog_fingerprint=preflight.catalog_fingerprint,
                        run_fingerprint=preflight.run_fingerprint,
                        pins={
                            "parser_version": preflight.parser_version,
                            "transform_version": preflight.transform_version,
                            "validation_policy": preflight.validation_policy,
                        },
                    )
                )
            revision = revision_start + (selected_index - cursor)
            self._commit_revision(
                preflight=preflight,
                run_id=run_id,
                revision=revision,
                outcomes=list(outcomes),
                logical_attempts=list(logical_attempts),
                physical_attempts=list(physical_attempts),
                idempotency_key=idempotency_key,
                gen=expected_generation,
            )
            self._heartbeat_lease(run_dir, expected_generation)
        # Finalize
        report = _read_json(run_dir / "report.json")
        status = _read_json(run_dir / "status.json")
        return RunResult(
            run_id=run_id,
            status=str(status.get("status", report.get("status", "unknown"))),
            revision=int(status.get("revision", report.get("revision", 0))),
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
    def _annotate_outcome(
        outcome: Mapping[str, Any],
        *,
        run_id: str,
        catalog_fingerprint: str,
        run_fingerprint: str,
        pins: Mapping[str, Any],
    ) -> dict[str, Any]:
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
        value["run_id"] = run_id
        value["catalog_fingerprint"] = catalog_fingerprint
        value["run_fingerprint"] = run_fingerprint
        value["pins"] = dict(pins)
        return value

    @staticmethod
    def _annotate_physical_attempt(
        attempt: Mapping[str, Any],
        *,
        catalog_fingerprint: str,
        run_fingerprint: str,
        pins: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Attach immutable run identity to each physical observation row."""
        value = dict(attempt)
        value["catalog_fingerprint"] = catalog_fingerprint
        value["run_fingerprint"] = run_fingerprint
        value["pins"] = dict(pins)
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
                "catalog_fingerprint": preflight.catalog_fingerprint,
                "source_ref": item.get("source_ref"),
                "source_role": item.get("source_role"),
                "authority_role": item.get("authority_role"),
                "pins": {
                    "parser_version": preflight.parser_version,
                    "transform_version": preflight.transform_version,
                    "validation_policy": preflight.validation_policy,
                },
                "raw_hash": item.get("raw_hash"),
                "raw_content_hash": item.get("raw_hash"),
                "canonical_hash": item.get("canonical_content_hash"),
                "canonical_content_hash": item.get("canonical_content_hash"),
                "raw_snapshot_id": item.get("raw_snapshot_id"),
                "observation_id": item.get("observation_id"),
                "attempt_id": item.get("attempt_id"),
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
        is_interrupted = status == "interrupted"
        completion: dict[str, Any]
        if is_interrupted:
            completion = {
                "state": "interrupted",
                "terminal": False,
                "resumable": True,
                "all_outcomes_terminal": False,
                "checkpoint_complete": False,
                "eligible": False,
                "reasons": ["interrupted"],
            }
        else:
            completion = {
                "state": "blocked" if status == "blocked" else "completed",
                "terminal": True,
                "resumable": False,
                "all_outcomes_terminal": pending_count == 0,
                "checkpoint_complete": checkpoint_complete,
                "eligible": eligible,
                "reasons": completion_reasons,
            }
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
            "completion": completion,
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
                    "attempt_id",
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
                    or len(outcomes) < len(preflight.selected)
                )
                if len(outcomes) < len(preflight.selected)
                else (
                    len(preflight.selected)
                    == len({item["stable_key"] for item in outcomes})
                    == sum(outcome_counts.values())
                    and set(preflight.selected_keys[: len(outcomes)])
                    == {item["stable_key"] for item in accepted_joins}.union(
                        {item["stable_key"] for item in exclusions}
                    )
                ),
            },
        }
