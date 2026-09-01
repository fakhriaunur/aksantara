"""Fail-closed candidate evaluation for checkpoint runs."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from aksantara.domain.errors import AksantaraDomainError
from aksantara.domain.models import SourceRef
from aksantara.domain.provenance import (
    CANONICAL_RECORD_FIELDS,
    canonical_content_hash,
    canonical_json_hash,
    canonical_record_bytes,
    canonical_record_payload,
    content_hash_bytes,
)
from aksantara.ingest.checkpoint_storage import (
    _hash_payload,
    _read_json,
    _safe_relative,
    _write_state_json,
)
from aksantara.ingest.checkpoint_types import (
    _TERMINAL_OUTCOMES,
    CheckpointError,
    CheckpointPersistenceError,
)
from aksantara.ingest.snapshots import RawSnapshotStore
from aksantara.parse.parser_contract import ParserError, parse_kbbi
from aksantara.validate.review import ReviewStore
from aksantara.validate.schema import validate_entry


def _coerce_int(value: Any, field: str) -> int:
    """Coerce a stored durable int that may be string-encoded."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            try:
                return int(stripped)
            except (ValueError, TypeError):
                pass
    raise CheckpointPersistenceError(
        f"malformed durable integer for {field}",
        details={"field": field, "value": value},
    )


def _coerce_outcome_index(value: Any) -> int | None:
    """Coerce selected_index for sorting; returns None on malformed."""
    try:
        return _coerce_int(value, "selected_index")
    except CheckpointPersistenceError:
        return None


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_REF_FIELDS = frozenset(
    {
        "url",
        "source_kind",
        "edition",
        "source_version",
        "retrieved_at",
        "content_hash",
        "parser_version",
    }
)
_PIN_FIELDS = frozenset({"parser_version", "transform_version", "validation_policy"})


class CheckpointCandidateMixin:
    """Evaluate exact joins without mutating canonical or release state."""

    root: Path

    def evaluate_candidate(  # noqa: C901
        self: Any,
        run_id: str,
        *,
        release_approved: bool = False,
        release_reviewer: str | None = None,
        release_reason: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate and, only when fully approved, persist one candidate.

        Candidate evaluation is fail-closed.  It never mutates canonical
        entries, raw snapshots, review evidence, vectors, or the current
        release pointer.  An item can be admitted only when it has an
        adapter-verified official source, a terminal accepted outcome (or an
        explicitly resolved official conflict), and exact parsed/hash joins.
        The complete candidate additionally requires a fixed 100-key
        checkpoint and explicit release-level human approval.
        """
        if not isinstance(release_approved, bool):
            raise CheckpointError("release_approved must be a boolean")
        if release_reviewer is not None and (
            not isinstance(release_reviewer, str) or not release_reviewer.strip()
        ):
            raise CheckpointError("release_reviewer must be non-empty when provided")
        if release_reason is not None and (
            not isinstance(release_reason, str) or not release_reason.strip()
        ):
            raise CheckpointError("release_reason must be non-empty when provided")

        # Snapshot upstream state so we can guarantee no mutation on failure.
        # Structured errors must leave canonical, candidate, release, pointer,
        # review, and history state unchanged.
        try:
            run_dir = self._existing_run_dir(run_id)
            report = _read_json(run_dir / "report.json")
            checkpoint = _read_json(run_dir / "checkpoint.json")
            outcomes_payload = _read_json(run_dir / "outcomes.json")
            attempts_payload = _read_json(run_dir / "attempts.json")
            status = _read_json(run_dir / "status.json")
            request = _read_json(run_dir / "request.json")
            preflight = _read_json(run_dir / "preflight.json")
        except CheckpointError:
            raise
        except AksantaraDomainError as exc:
            raise CheckpointError(
                str(exc), details={"reason": getattr(exc, "reason", str(exc))}
            ) from exc
        except Exception as exc:
            raise CheckpointPersistenceError(
                "failed to load durable checkpoint artifacts",
                details={"run_id": run_id, "error_type": type(exc).__name__},
            ) from exc

        outcomes = outcomes_payload.get("outcomes", [])
        if not isinstance(outcomes, list):
            raise CheckpointPersistenceError(
                "outcomes artifact does not contain an array",
                details={"run_id": run_id},
            )
        try:
            context = self._candidate_lineage_context(
                run_id=run_id,
                report=report,
                checkpoint=checkpoint,
                outcomes_payload=outcomes_payload,
                attempts_payload=attempts_payload,
                status=status,
                preflight=preflight,
                request=request,
            )
        except CheckpointError:
            raise
        except AksantaraDomainError as exc:
            raise CheckpointError(
                str(exc), details={"reason": getattr(exc, "reason", str(exc))}
            ) from exc

        run_fingerprint = context["run_fingerprint"]
        catalog_fingerprint = context["catalog_fingerprint"]
        pins = context["pins"]
        state_reason = context["state_reason"]
        # Domain validation errors including QuarantinedError are caught
        # and surfaced as structured CheckpointError or ineligible.
        try:
            reviews = [
                review
                for review in ReviewStore(root=self.root).list_all()
                if str(review.get("first_seen_run", "")) == run_id
            ]
        except AksantaraDomainError as exc:
            raise CheckpointError(
                str(exc), details={"reason": getattr(exc, "reason", str(exc))}
            ) from exc

        review_by_key: dict[str, list[dict[str, Any]]] = {}
        for review in reviews:
            key = str(review.get("stable_key", ""))
            if key:
                review_by_key.setdefault(key, []).append(review)

        eligible_items: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        # Conservation: exactly one current outcome per immutable preflight-selected key
        # with set-based checks. Use coercion for selected_index.
        selected_keys_expected: list[str] = []
        try:
            # Preflight is already validated in context, but re-derive for set checks
            sel = preflight.get("selection", {})
            if isinstance(sel.get("selected_keys"), list):
                selected_keys_expected = [str(k) for k in sel.get("selected_keys", [])]
        except Exception:
            selected_keys_expected = []

        # Detect malformed selected_index coercion failures early
        malformed_index_count = 0
        sortable_outcomes: list[tuple[int, dict[str, Any]]] = []
        for value in outcomes:
            if not isinstance(value, Mapping):
                continue
            raw_idx = value.get("selected_index", 0)
            coerced = _coerce_outcome_index(raw_idx)
            if coerced is None:
                # Malformed selected_index -> structured ineligible, not 500
                malformed_index_count += 1
                coerced = 0
            sortable_outcomes.append((coerced, dict(value)))
        # If any malformed, state_reason becomes persistence/ineligible
        if malformed_index_count and state_reason is None:
            # Treat malformed durable index as checkpoint_state_mismatch (ineligible)
            # rather than 500, per malformed durable normalization
            state_reason = "checkpoint_state_mismatch"

        # Check set-based conservation before per-item joins
        # This is the complete run-pin and fingerprint join plus conservation gate
        if state_reason is None:
            outcome_keys = [str(v.get("stable_key", "")) for _, v in sortable_outcomes]
            # Truncated / missing / extra / duplicate
            if len(outcome_keys) != len(selected_keys_expected):
                state_reason = "checkpoint_state_mismatch"
            elif len(set(outcome_keys)) != len(outcome_keys):
                state_reason = "checkpoint_state_mismatch"
            elif set(outcome_keys) != set(selected_keys_expected):
                state_reason = "checkpoint_state_mismatch"
            else:
                # Attempt ledger coverage: logical and physical
                try:
                    logical = (
                        attempts_payload.get("attempts")
                        if isinstance(attempts_payload.get("attempts"), list)
                        else []
                    )
                    if len(logical) != len(selected_keys_expected):  # type: ignore[arg-type]
                        state_reason = "checkpoint_state_mismatch"
                    # Physical coverage is validated per-item to preserve
                    # specific observation_attempt_missing reasons; global
                    # incomplete physical ledger is still ineligible via
                    # per-item checks, not a blanket state_reason.
                except Exception:
                    state_reason = "checkpoint_state_mismatch"

        for _, item in sorted(
            sortable_outcomes, key=lambda kv: (str(kv[1].get("stable_key", "")), kv[0])
        ):
            stable_key = str(item.get("stable_key", ""))
            item_reviews = review_by_key.get(stable_key, [])
            try:
                item_eligible, reason, candidate_item = self._candidate_item(
                    item,
                    item_reviews,
                    run_dir=run_dir,
                    run_fingerprint=run_fingerprint,
                    catalog_fingerprint=catalog_fingerprint,
                    pins=pins,
                    report=report,
                    attempts_payload=attempts_payload,
                    state_reason=state_reason,
                )
            except AksantaraDomainError as exc:
                # Domain validation errors including QuarantinedError are normalized
                # to structured ineligible, not 500. Preserve machine-readable reason.
                item_eligible, reason, candidate_item = (
                    False,
                    getattr(exc, "reason", "quarantined"),
                    None,
                )
            except CheckpointError:
                raise
            except Exception as exc:
                raise CheckpointPersistenceError(
                    "candidate item evaluation failed",
                    details={
                        "stable_key": stable_key,
                        "error_type": type(exc).__name__,
                    },
                ) from exc
            if item_eligible and candidate_item is not None:
                eligible_items.append(candidate_item)
            else:
                excluded.append(
                    {
                        "stable_key": stable_key,
                        "entry_id": item.get("entry_id"),
                        "reason": reason or "not_eligible",
                        "outcome": item.get("outcome"),
                        "review_ids": [
                            str(review.get("review_id"))
                            for review in item_reviews
                            if review.get("review_id")
                        ],
                    }
                )

        # Durable lifecycle artifacts are independently validated: cursor/limit/selected_count
        # This was already checked in _candidate_lineage_context, but recompute checkpoint_complete
        # with coerced selected_count handling.
        selected_count_raw = checkpoint.get("selected_count", len(outcomes))
        try:
            selected_count_coerced = _coerce_int(selected_count_raw, "selected_count")
        except CheckpointPersistenceError:
            selected_count_coerced = len(outcomes) + 1  # force mismatch
            if state_reason is None:
                state_reason = "checkpoint_state_mismatch"

        checkpoint_complete = bool(
            context["fixed_checkpoint"]
            and report.get("completion", {}).get("checkpoint_complete")
            and report.get("status") == "completed"
            and len(outcomes) == selected_count_coerced
            and selected_count_coerced == len(selected_keys_expected)
            and all(
                isinstance(item, Mapping)
                and item.get("outcome")
                in {"accepted", "quarantined", "rejected", "failed"}
                for _, item in sortable_outcomes
            )
            and state_reason is None
        )
        reasons: list[str] = []
        if state_reason:
            reasons.append(state_reason)
        if not checkpoint_complete:
            reasons.append("checkpoint_incomplete")
        if excluded:
            reasons.append("excluded_items")
        if not release_approved:
            reasons.append("release_approval_required")
        elif not release_reviewer or not release_reviewer.strip():
            reasons.append("release_reviewer_required")
        elif not release_reason or not release_reason.strip():
            reasons.append("release_reason_required")
        if len(eligible_items) != len(outcomes):
            # Keep this separate from excluded_items so callers can explain
            # the exact conservation failure without inferring from counts.
            reasons.append("candidate_set_incomplete")
        eligible = not reasons

        # Atomic candidate and evaluation persistence: stage and commit only
        # with successful evaluation; orphan candidates are never independently
        # discoverable and state is preserved on failure.
        selected_keys = (
            [
                str(v.get("stable_key"))
                for _, v in sortable_outcomes
                if isinstance(v, Mapping)
            ]
            if sortable_outcomes
            else [
                str(item.get("stable_key"))
                for item in outcomes
                if isinstance(item, Mapping)
            ]
        )
        eligible_keys = [
            str(item.get("entry_id")) for item in eligible_items if item.get("entry_id")
        ]
        excluded_keys = [
            str(item.get("stable_key")) for item in excluded if item.get("stable_key")
        ]
        conservation = {
            "selected_count": len(outcomes),
            "eligible_count": len(eligible_items),
            "excluded_count": len(excluded),
            "partition_holds": len(eligible_items) + len(excluded) == len(outcomes),
        }
        evaluation: dict[str, Any] = {
            "schema_version": "checkpoint-candidate-evaluation-v1",
            "run_id": run_id,
            "run_fingerprint": run_fingerprint,
            "candidate_created": False,
            "candidate": None,
            "eligible": eligible,
            "checkpoint_complete": checkpoint_complete,
            "release_approval": {
                "approved": release_approved,
                "reviewer": (release_reviewer or "").strip(),
                "reason": (release_reason or "").strip(),
            },
            "eligible_count": len(eligible_items),
            "selected_count": len(outcomes),
            "selected_keys": selected_keys,
            "selected_ids": selected_keys,
            "eligible_keys": eligible_keys,
            "eligible_ids": eligible_keys,
            "excluded": excluded,
            "excluded_keys": excluded_keys,
            "excluded_ids": excluded_keys,
            "blocked_ids": excluded_keys,
            "reason_codes": reasons,
            "predicate": {
                "name": "checkpoint-candidate-exact-lineage-v1",
                "terminal_outcomes": sorted(_TERMINAL_OUTCOMES),
                "requires_release_approval": True,
                "requires_official_source": True,
                "requires_exact_joins": [
                    "run_id",
                    "run_fingerprint",
                    "catalog_fingerprint",
                    "parser_version",
                    "transform_version",
                    "validation_policy",
                    "source_ref",
                    "authority_role",
                    "observation_id",
                    "raw_snapshot_id",
                    "raw_sha256",
                    "raw_reference",
                    "attempt_id",
                    "raw_content_hash",
                    "parsed_reference",
                    "canonical_reference",
                    "canonical_content_hash",
                    "canonical_serialization",
                    "entry_id",
                ],
            },
            "conservation": conservation,
            "current_version_changed": False,
            "vector_work": False,
        }

        if eligible:
            candidate_payload: dict[str, Any] = {
                "schema_version": "checkpoint-candidate-v1",
                "candidate_id": f"candidate-{run_fingerprint}",
                "run_id": run_id,
                "run_fingerprint": run_fingerprint,
                "catalog_fingerprint": catalog_fingerprint,
                "corpus": report.get("corpus", {}),
                "selection": report.get("selection", {}),
                "pins": pins,
                "authority": {
                    "mode": "official-first",
                    "official_source_kinds": [
                        "official-live",
                        "official-snapshot",
                    ],
                    "fallback_is_evidence_only": True,
                },
                "release_approval": {
                    "approved": True,
                    "reviewer": (release_reviewer or "").strip(),
                    "reason": (release_reason or "").strip(),
                },
                "entries": eligible_items,
                "vectors_created": False,
                "pointer_changed": False,
            }
            candidate_payload["self_hash"] = _hash_payload(candidate_payload)
            candidate_bytes = (
                json.dumps(
                    candidate_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            candidate_path = (
                self.root
                / ".aksantara"
                / "candidates"
                / f"{candidate_payload['candidate_id']}.json"
            )
            # Stage candidate bytes and evaluation atomically
            candidates_dir = candidate_path.parent
            evaluation_path = run_dir / "candidate-evaluation.json"
            # Prepare evaluation with candidate included for commit
            evaluation["candidate_created"] = True
            evaluation["candidate"] = {
                **candidate_payload,
                "reference": _safe_relative(self.root, candidate_path),
                "candidate_created": True,
            }
            # Staging paths (hidden, not discoverable)
            staging_candidate = (
                candidates_dir / f".{candidate_path.name}.tmp.{os.getpid()}"
            )
            staging_evaluation = (
                run_dir / f".candidate-evaluation.json.tmp.{os.getpid()}"
            )
            try:
                candidates_dir.mkdir(parents=True, exist_ok=True)
                staging_candidate.write_bytes(candidate_bytes)
                # Write evaluation staging
                staging_evaluation.write_bytes(
                    json.dumps(
                        evaluation,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
            except OSError as exc:
                for p in (staging_candidate, staging_evaluation):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise CheckpointPersistenceError(
                    "candidate staging failed",
                    details={"run_id": run_id, "error_type": type(exc).__name__},
                ) from exc
            try:
                # Commit both atomically: candidate first is staged not visible,
                # evaluation is the gate. Use replace for atomic visibility.
                # Candidate is not discoverable until evaluation commits.
                os.replace(staging_candidate, candidate_path)
                os.replace(staging_evaluation, evaluation_path)
            except OSError as exc:
                for p in (staging_candidate, staging_evaluation):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                # Preserve state on failure: remove partially committed candidate if evaluation not yet
                # If candidate was already replaced but evaluation replace failed, remove candidate to avoid orphan
                try:
                    if evaluation_path.exists():
                        # evaluation replace failed, candidate may have been committed orphan
                        # Remove orphan candidate to keep atomicity
                        candidate_path.unlink(missing_ok=True)
                    else:
                        candidate_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise CheckpointPersistenceError(
                    "candidate commit failed",
                    details={"run_id": run_id, "error_type": type(exc).__name__},
                ) from exc
            return evaluation
        else:
            # Non-eligible: no candidate bytes, but evaluation must be persisted.
            # Preserve atomicity via _write_state_json (which is atomic) and leave
            # candidate state unchanged (no orphan created).
            evaluation_path = run_dir / "candidate-evaluation.json"
            try:
                _write_state_json(evaluation_path, evaluation, self.root)
            except CheckpointError:
                raise
            except Exception as exc:
                raise CheckpointPersistenceError(
                    "evaluation persistence failed",
                    details={"run_id": run_id, "error_type": type(exc).__name__},
                ) from exc
            return evaluation

    def _candidate_lineage_context(  # noqa: C901
        self: Any,
        *,
        run_id: str,
        report: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        outcomes_payload: Mapping[str, Any],
        attempts_payload: Mapping[str, Any],
        status: Mapping[str, Any],
        preflight: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Load and cross-check immutable run identity before item joins."""
        context: dict[str, Any] = {
            "run_fingerprint": "",
            "catalog_fingerprint": "",
            "pins": {},
            "state_reason": None,
            "fixed_checkpoint": False,
        }
        # Fingerprint and preimage checks with structured malformed handling
        try:
            preflight_fingerprints = preflight.get("fingerprints")
            if not isinstance(preflight_fingerprints, Mapping):
                context["state_reason"] = "run_fingerprint_missing"
                return context
            if request.get("run_id") != run_id:
                context["state_reason"] = "run_id_mismatch"
                return context
            request_preflight = request.get("preflight")
            if not isinstance(request_preflight, Mapping) or dict(
                request_preflight
            ) != dict(preflight):
                context["state_reason"] = "run_fingerprint_mismatch"
                return context
            run_fingerprint = preflight_fingerprints.get("run")
            catalog_fingerprint = preflight_fingerprints.get("catalog")
            if not _valid_hash(run_fingerprint):
                context["state_reason"] = "run_fingerprint_missing"
                return context
            if not _valid_hash(catalog_fingerprint):
                context["state_reason"] = "catalog_fingerprint_missing"
                return context
            context["run_fingerprint"] = str(run_fingerprint)
            context["catalog_fingerprint"] = str(catalog_fingerprint)

            preimages = preflight_fingerprints.get("preimages")
            if not isinstance(preimages, Mapping):
                context["state_reason"] = "run_fingerprint_unverifiable"
                return context
            catalog_preimage = preimages.get("catalog")
            run_preimage = preimages.get("run")
            if not isinstance(catalog_preimage, Mapping) or not isinstance(
                run_preimage, Mapping
            ):
                context["state_reason"] = "run_fingerprint_unverifiable"
                return context
            if _hash_payload(catalog_preimage) != str(
                catalog_fingerprint
            ) or _hash_payload(run_preimage) != str(run_fingerprint):
                context["state_reason"] = "run_fingerprint_mismatch"
                return context
            # Pins mapping validation: malformed durable pins are persistence/ineligible
            pins_obj = preflight.get("pins")
            if not isinstance(pins_obj, Mapping):
                context["state_reason"] = "run_pins_missing"
                return context
            if (
                run_preimage.get("catalog_fingerprint") != catalog_fingerprint
                or run_preimage.get("parser_version") != pins_obj.get("parser_version")
                or run_preimage.get("transform_version")
                != pins_obj.get("transform_version")
                or run_preimage.get("validation_policy")
                != pins_obj.get("validation_policy")
            ):
                context["state_reason"] = "run_fingerprint_mismatch"
                return context
            selection = preflight.get("selection")
            records = preflight.get("records")
            if not isinstance(selection, Mapping) or not isinstance(records, list):
                context["state_reason"] = "checkpoint_state_mismatch"
                return context
            record_keys = [
                value.get("stable_key")
                for value in records
                if isinstance(value, Mapping)
            ]
            selected_keys = selection.get("selected_keys")
            effective_limit_raw = run_preimage.get("effective_limit")
            # Coercion for effective_limit and selected counts
            try:
                effective_limit = _coerce_int(effective_limit_raw, "effective_limit")
            except CheckpointPersistenceError:
                context["state_reason"] = "checkpoint_state_mismatch"
                return context
            # Need to handle selected_keys being list but also check selected_count coercion
            try:
                selected_count_raw = selection.get("selected_count")
                shortfall_raw = selection.get("shortfall")
                selected_count = (
                    _coerce_int(selected_count_raw, "selected_count")
                    if selected_count_raw is not None
                    else len(selected_keys)
                    if isinstance(selected_keys, list)
                    else 0
                )
                shortfall = (
                    _coerce_int(shortfall_raw, "shortfall")
                    if shortfall_raw is not None
                    else max(0, effective_limit - len(record_keys))
                )
            except CheckpointPersistenceError:
                context["state_reason"] = "checkpoint_state_mismatch"
                return context
            if (
                not isinstance(selected_keys, list)
                or effective_limit
                != _coerce_int(selection.get("effective_limit"), "effective_limit")
                if isinstance(selection.get("effective_limit"), (int, str))
                else False
                or selected_keys != record_keys[:effective_limit]
                or selected_count != len(selected_keys)
                or shortfall != max(0, effective_limit - len(record_keys))
            ):
                context["state_reason"] = "checkpoint_state_mismatch"
                return context
            # Also check effective_limit matches selection.effective_limit with coercion
            try:
                sel_eff = _coerce_int(
                    selection.get("effective_limit"), "selected_count"
                )
                if sel_eff != effective_limit:
                    context["state_reason"] = "checkpoint_state_mismatch"
                    return context
            except CheckpointPersistenceError:
                context["state_reason"] = "checkpoint_state_mismatch"
                return context
            context["fixed_checkpoint"] = (
                effective_limit == 100
                and len(record_keys) >= 100
                and selected_count == 100
                and shortfall == 0
            )
            idempotency = request.get("idempotency")
            if not isinstance(idempotency, Mapping) or idempotency.get(
                "preimage"
            ) != dict(run_preimage):
                context["state_reason"] = "run_fingerprint_mismatch"
                return context

            report_fingerprints = report.get("fingerprints")
            if not _matching_fingerprints(
                report_fingerprints,
                catalog_fingerprint=str(catalog_fingerprint),
                run_fingerprint=str(run_fingerprint),
            ):
                context["state_reason"] = "run_fingerprint_mismatch"
                return context
            for artifact in (checkpoint, outcomes_payload, attempts_payload, status):
                if artifact.get("run_id") != run_id:
                    context["state_reason"] = "run_id_mismatch"
                    return context
                if not _matching_fingerprints(
                    artifact.get("fingerprints"),
                    catalog_fingerprint=str(catalog_fingerprint),
                    run_fingerprint=str(run_fingerprint),
                ):
                    context["state_reason"] = "run_fingerprint_mismatch"
                    return context

            # Durable lifecycle artifacts are independently validated
            # Status cursor/limit/selected_count, checkpoint selected_keys/terminal_count,
            # report processed/current/terminal/pending counters, and allowed lifecycle state
            try:
                # Status checks
                status_cursor = status.get("cursor", {})
                if isinstance(status_cursor, Mapping):
                    cursor_val_raw = status_cursor.get(
                        "value", status_cursor.get("cursor", 0)
                    )
                    cursor_lim_raw = status_cursor.get(
                        "limit", status.get("limit", effective_limit)
                    )
                    try:
                        cursor_val = (
                            _coerce_int(cursor_val_raw, "cursor_value")
                            if cursor_val_raw is not None
                            else 0
                        )
                        cursor_lim = (
                            _coerce_int(cursor_lim_raw, "cursor_limit")
                            if cursor_lim_raw is not None
                            else effective_limit
                        )
                    except CheckpointPersistenceError:
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
                    outcomes_list = outcomes_payload.get("outcomes", [])
                    outcomes_len = (
                        len(outcomes_list) if isinstance(outcomes_list, list) else 0
                    )
                    if cursor_val != outcomes_len:
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
                    if cursor_lim != effective_limit:
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
                status_selected_raw = status.get("selected_count")
                if status_selected_raw is not None:
                    try:
                        sc = _coerce_int(status_selected_raw, "selected_count")
                        if sc != len(selected_keys):  # type: ignore[arg-type]
                            context["state_reason"] = "checkpoint_state_mismatch"
                            return context
                    except CheckpointPersistenceError:
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
                # Allowed lifecycle state
                allowed_states = {
                    "created",
                    "running",
                    "blocked",
                    "failed",
                    "completed",
                }
                report_status = report.get("status")
                status_status = status.get("status")
                if (
                    report_status not in allowed_states
                    or status_status not in allowed_states
                ):
                    context["state_reason"] = "checkpoint_state_mismatch"
                    return context
                if report_status != status_status:
                    context["state_reason"] = "checkpoint_state_mismatch"
                    return context
                # Checkpoint selected_keys/terminal_count
                ckpt_selected = checkpoint.get("selected_keys")
                if isinstance(ckpt_selected, list):
                    if [str(k) for k in ckpt_selected] != [  # type: ignore[union-attr]
                        str(k)
                        for k in selected_keys  # type: ignore[union-attr]
                    ]:
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
                else:
                    # If missing, it's a mismatch unless selected_keys is empty
                    if selected_keys:  # type: ignore[truthy-bool]
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
                # terminal_count vs sum terminal outcomes
                outcomes_payload_list = outcomes_payload.get("outcomes", [])
                if isinstance(outcomes_payload_list, list):
                    term_expected = sum(
                        1
                        for it in outcomes_payload_list
                        if isinstance(it, Mapping)
                        and str(it.get("outcome"))
                        in {"accepted", "quarantined", "rejected", "failed"}
                    )
                    try:
                        term_actual_raw = checkpoint.get("terminal_count")
                        if term_actual_raw is not None:
                            term_actual = _coerce_int(term_actual_raw, "terminal_count")
                            if term_actual != term_expected:
                                context["state_reason"] = "checkpoint_state_mismatch"
                                return context
                    except CheckpointPersistenceError:
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
                    # report counters
                    try:
                        processed_raw = report.get("processed_count")
                        current_raw = report.get(
                            "current_outcome_count", len(outcomes_payload_list)
                        )
                        terminal_raw = report.get("terminal_count", term_expected)
                        pending_raw = report.get("pending_count", 0)
                        if processed_raw is not None:
                            proc = _coerce_int(processed_raw, "processed_count")
                            if proc != len(outcomes_payload_list):
                                context["state_reason"] = "checkpoint_state_mismatch"
                                return context
                        if current_raw is not None:
                            cur = _coerce_int(current_raw, "current_outcome_count")
                            if cur != len(outcomes_payload_list):
                                context["state_reason"] = "checkpoint_state_mismatch"
                                return context
                        if terminal_raw is not None:
                            term_r = _coerce_int(terminal_raw, "terminal_count")
                            if term_r != term_expected:
                                context["state_reason"] = "checkpoint_state_mismatch"
                                return context
                        if pending_raw is not None:
                            pend = _coerce_int(pending_raw, "pending_count")
                            pending_expected = sum(
                                1
                                for it in outcomes_payload_list
                                if isinstance(it, Mapping)
                                and str(it.get("outcome"))
                                in {"pending", "in_progress", "retryable"}
                            )
                            if pend != pending_expected:
                                context["state_reason"] = "checkpoint_state_mismatch"
                                return context
                    except CheckpointPersistenceError:
                        context["state_reason"] = "checkpoint_state_mismatch"
                        return context
            except CheckpointError:
                raise
            except Exception:
                context["state_reason"] = "checkpoint_state_mismatch"
                return context

            if (
                report.get("run_id") != run_id
                or report.get("revision") != checkpoint.get("revision")
                or report.get("revision") != outcomes_payload.get("revision")
                or report.get("revision") != status.get("revision")
                or report.get("status") != status.get("status")
                or report.get("selection") != selection
                or not _matching_corpus(
                    report.get("corpus"),
                    preflight.get("catalog"),
                    record_count=len(records),
                )
                or _coerce_int(checkpoint.get("processed_count"), "processed_count")
                != len(outcomes_payload.get("outcomes", []))
                or status.get("outcome_counts") != checkpoint.get("outcome_counts")
                or status.get("completion") != report.get("completion")
                or attempts_payload.get("revision") != report.get("revision")
                or _coerce_int(attempts_payload.get("attempt_count"), "attempt_count")
                != len(attempts_payload.get("physical_attempts", []))
                or _coerce_int(
                    attempts_payload.get("logical_attempt_count"),
                    "logical_attempt_count",
                )
                != len(attempts_payload.get("attempts", []))
            ):
                context["state_reason"] = "checkpoint_state_mismatch"
                return context

            preflight_pins = preflight.get("pins")
            if not isinstance(preflight_pins, Mapping) or not _valid_pins(
                preflight_pins
            ):
                context["state_reason"] = "run_pins_missing"
                return context
            preflight_pins_mapping = cast(Mapping[str, Any], preflight_pins)
            expected_pins = {
                key: str(preflight_pins_mapping.get(key)) for key in sorted(_PIN_FIELDS)
            }
            for artifact in (
                report,
                checkpoint,
                outcomes_payload,
                attempts_payload,
                status,
            ):
                artifact_pins = artifact.get("pins")
                if not isinstance(artifact_pins, Mapping) or not _valid_pins(
                    artifact_pins
                ):
                    context["state_reason"] = "run_pins_missing"
                    return context
                pin_reason = _pin_mismatch_reason(artifact_pins, expected_pins)
                if pin_reason is not None:
                    context["state_reason"] = pin_reason
                    return context
            context["pins"] = expected_pins
            return context
        except CheckpointError:
            raise
        except AksantaraDomainError as exc:
            raise CheckpointError(
                str(exc), details={"reason": getattr(exc, "reason", str(exc))}
            ) from exc
        except Exception as exc:
            raise CheckpointPersistenceError(
                "candidate lineage context failed",
                details={"run_id": run_id, "error_type": type(exc).__name__},
            ) from exc

    def _candidate_item(
        self: Any,
        item: Mapping[str, Any],
        reviews: list[dict[str, Any]],
        *,
        run_dir: Path,
        run_fingerprint: str,
        catalog_fingerprint: str,
        pins: Mapping[str, Any],
        report: Mapping[str, Any],
        attempts_payload: Mapping[str, Any],
        state_reason: str | None,
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        """Validate one outcome and materialize its exact candidate join."""
        if state_reason is not None:
            return False, state_reason, None
        header_reason, stable_key, outcome = _candidate_header_reason(
            item,
            run_id=run_dir.name,
            run_fingerprint=run_fingerprint,
            pins=pins,
            report=report,
        )
        if header_reason is not None or stable_key is None:
            return False, header_reason or "not_eligible", None

        resolved_conflict = _resolved_conflict(reviews)
        if outcome != "accepted" and resolved_conflict is None:
            return (
                False,
                str(item.get("exclusion_reason", outcome or "not_eligible")),
                None,
            )
        review_reason = _blocking_review_reason(reviews, resolved_conflict)
        if review_reason is not None:
            return False, review_reason, None

        source_ref_value = item.get("source_ref")
        source_ref = _source_ref_payload(source_ref_value)
        if source_ref is None:
            return False, "source_ref_missing", None
        source_reason = _official_source_reason(item, source_ref, pins)
        if source_reason is not None:
            return False, source_reason, None

        if outcome == "accepted":
            report_join = _find_by_key(report.get("accepted_joins"), stable_key)
            if report_join is None:
                return False, "accepted_join_missing", None
            report_join_reason = _accepted_join_reason(
                report_join,
                item=item,
                run_id=run_dir.name,
                run_fingerprint=run_fingerprint,
                catalog_fingerprint=catalog_fingerprint,
                source_ref=source_ref,
            )
            if report_join_reason is not None:
                return False, report_join_reason, None

        attempt_reason, attempt_id, attempt = self._candidate_attempt_context(
            attempts_payload,
            item=item,
            stable_key=stable_key,
            source_ref=source_ref,
            run_id=run_dir.name,
            run_fingerprint=run_fingerprint,
            pins=pins,
        )
        if attempt_reason is not None or attempt_id is None or attempt is None:
            return False, attempt_reason or "observation_attempt_missing", None

        parsed_reason, parsed = self._candidate_parsed_context(
            item,
            run_dir=run_dir,
            run_fingerprint=run_fingerprint,
            catalog_fingerprint=catalog_fingerprint,
            pins=pins,
            source_ref=source_ref,
            stable_key=stable_key,
            attempt_id=attempt_id,
        )
        if parsed_reason is not None or parsed is None:
            return False, parsed_reason or "parsed_join_missing", None

        raw_reason, raw = self._candidate_raw_context(
            item,
            run_dir=run_dir,
            source_ref=source_ref,
            normalized_entry_source=parsed["source"],
        )
        if raw_reason is not None or raw is None:
            return False, raw_reason or "raw_snapshot_missing", None

        raw_hash = raw["raw_hash"]
        raw_snapshot_id = raw["raw_snapshot_id"]
        raw_bytes = raw["raw_bytes"]
        raw_reference = raw["raw_reference"]
        observation_id = item.get("observation_id")
        observation_reason = self._observation_join_reason(
            source_ref=source_ref,
            source_role=str(item.get("source_role", "official")),
            raw_hash=raw_hash,
            raw_snapshot_id=raw_snapshot_id,
            observation_id=observation_id,
            attempt_id=attempt_id,
            raw_reference=raw_reference,
        )
        if observation_reason is not None:
            return False, observation_reason, None
        if _reparse_raw_join(raw_bytes, source_ref, parsed["entry"]) is not None:
            return False, "parsed_raw_join_mismatch", None
        if resolved_conflict is not None:
            review_reason = _resolved_review_join_reason(
                resolved_conflict,
                run_id=run_dir.name,
                run_fingerprint=run_fingerprint,
                catalog_fingerprint=catalog_fingerprint,
                pins=pins,
                stable_key=stable_key,
                source_ref=source_ref,
                raw_hash=raw_hash,
                raw_snapshot_id=raw_snapshot_id,
                observation_id=observation_id,
                canonical_hash=parsed["expected_hash"],
                entry_id=parsed["entry_id"],
                parsed_reference=parsed["parsed_reference"],
                canonical_reference=parsed["canonical_reference"],
            )
            if review_reason is not None:
                return False, review_reason, None
        return (
            True,
            None,
            _candidate_payload(
                item=item,
                entry=parsed["entry"],
                source_ref=source_ref,
                run_fingerprint=run_fingerprint,
                catalog_fingerprint=catalog_fingerprint,
                pins=pins,
                raw_hash=raw_hash,
                raw_snapshot_id=raw_snapshot_id,
                observation_id=observation_id,
                attempt_id=attempt_id,
                expected_hash=parsed["expected_hash"],
                canonical_reference=parsed["canonical_reference"],
                parsed_reference=parsed["parsed_reference"],
                raw_reference=raw_reference,
                canonical_serialization=parsed["canonical_serialization"],
            ),
        )

    def _candidate_attempt_context(
        self: Any,
        attempts_payload: Mapping[str, Any],
        *,
        item: Mapping[str, Any],
        stable_key: str,
        source_ref: Mapping[str, Any],
        run_id: str,
        run_fingerprint: str,
        pins: Mapping[str, Any],
    ) -> tuple[str | None, str | None, Mapping[str, Any] | None]:
        """Validate the outcome's official attempt envelope."""
        official_observation = item.get("official_observation")
        if not isinstance(official_observation, Mapping):
            return "observation_attempt_missing", None, None
        attempt_id = item.get("attempt_id", official_observation.get("attempt_id"))
        if not isinstance(attempt_id, str) or not attempt_id:
            return "observation_attempt_missing", None, None
        if official_observation.get("attempt_id") != attempt_id:
            return "observation_attempt_mismatch", None, None
        observation_reason = _official_observation_reason(
            official_observation,
            item=item,
            run_id=run_id,
            source_ref=source_ref,
            attempt_id=attempt_id,
        )
        if observation_reason is not None:
            return observation_reason, None, None
        logical_reason = _logical_attempt_reason(
            attempts_payload,
            item=item,
            stable_key=stable_key,
            run_id=run_id,
            run_fingerprint=run_fingerprint,
            pins=pins,
            attempt_id=attempt_id,
        )
        if logical_reason is not None:
            return logical_reason, None, None
        reason, attempt = _find_attempt(
            attempts_payload,
            attempt_id=attempt_id,
            stable_key=stable_key,
            source_ref=source_ref,
            item=item,
            run_id=run_id,
            catalog_fingerprint=str(
                attempts_payload.get("fingerprints", {}).get("catalog", "")
            ),
            run_fingerprint=run_fingerprint,
            pins=pins,
        )
        return reason, attempt_id, attempt

    def _candidate_parsed_context(
        self: Any,
        item: Mapping[str, Any],
        *,
        run_dir: Path,
        run_fingerprint: str,
        catalog_fingerprint: str,
        pins: Mapping[str, Any],
        source_ref: Mapping[str, Any],
        stable_key: str,
        attempt_id: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Validate the parsed artifact and its run/canonical joins."""
        expected_hash_value = item.get("canonical_content_hash")
        if not _valid_hash(expected_hash_value):
            return "canonical_hash_missing", None
        expected_hash = str(expected_hash_value)
        parsed_reference = item.get("parsed_reference")
        if not isinstance(parsed_reference, str) or not parsed_reference:
            return "parsed_join_missing", None
        parsed_path = (self.root / parsed_reference).resolve()
        if not _inside_run(parsed_path, self.root, run_dir):
            return "parsed_join_outside_run", None
        try:
            parsed_payload = _read_json(parsed_path)
        except CheckpointError:
            return "parsed_join_missing", None
        metadata_reason = _parsed_metadata_reason(
            parsed_payload,
            item=item,
            run_id=run_dir.name,
            run_fingerprint=run_fingerprint,
            catalog_fingerprint=catalog_fingerprint,
            pins=pins,
            source_ref=source_ref,
            stable_key=stable_key,
            attempt_id=attempt_id,
        )
        if metadata_reason is not None:
            return metadata_reason, None
        entry = parsed_payload.get("entry")
        entry_reason, normalized_source, entry_id = _parsed_entry_reason(
            entry,
            item=item,
            source_ref=source_ref,
            pins=pins,
            expected_hash=expected_hash,
        )
        if entry_reason is not None or normalized_source is None or entry_id is None:
            return entry_reason or "parsed_entry_missing", None
        entry_mapping = cast(Mapping[str, Any], entry)
        canonical_reference = item.get("canonical_reference")
        if not isinstance(canonical_reference, str) or not canonical_reference:
            return "canonical_join_missing", None
        canonical_reason = _canonical_join_reason(
            self.root,
            run_dir,
            canonical_reference,
            expected_hash,
            dict(entry_mapping),
        )
        if canonical_reason is not None:
            return canonical_reason, None
        return (
            None,
            {
                "parsed_payload": parsed_payload,
                "parsed_reference": parsed_reference,
                "entry": dict(entry_mapping),
                "entry_id": entry_id,
                "source": normalized_source,
                "expected_hash": expected_hash,
                "canonical_reference": canonical_reference,
                "canonical_serialization": parsed_payload.get(
                    "canonical_serialization"
                ),
            },
        )

    def _candidate_raw_context(
        self: Any,
        item: Mapping[str, Any],
        *,
        run_dir: Path,
        source_ref: Mapping[str, Any],
        normalized_entry_source: Mapping[str, Any],
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Validate the raw snapshot and per-run raw artifact joins."""
        raw_hash_value = item.get("raw_hash")
        if not _valid_hash(raw_hash_value):
            return "raw_hash_missing", None
        raw_hash = str(raw_hash_value)
        raw_snapshot_id = item.get("raw_snapshot_id")
        if not isinstance(raw_snapshot_id, str) or not raw_snapshot_id:
            return "raw_snapshot_missing", None
        try:
            raw_bytes = RawSnapshotStore(self.root).get(raw_snapshot_id)
        except (OSError, ValueError, FileNotFoundError):
            return "raw_snapshot_missing", None
        if content_hash_bytes(raw_bytes) != raw_hash:
            return "raw_hash_mismatch", None
        if source_ref["content_hash"] != raw_hash:
            return "raw_source_join_mismatch", None
        if normalized_entry_source["content_hash"] != raw_hash:
            return "raw_entry_join_mismatch", None
        raw_reference = item.get("raw_reference")
        if not isinstance(raw_reference, str) or not raw_reference:
            return "raw_join_missing", None
        raw_path = (self.root / raw_reference).resolve()
        if not _inside_run(raw_path, self.root, run_dir):
            return "raw_join_missing", None
        try:
            run_raw_bytes = raw_path.read_bytes()
        except (OSError, ValueError):
            return "raw_join_missing", None
        if run_raw_bytes != raw_bytes or content_hash_bytes(run_raw_bytes) != raw_hash:
            return "raw_hash_mismatch", None
        return (
            None,
            {
                "raw_hash": raw_hash,
                "raw_snapshot_id": raw_snapshot_id,
                "raw_bytes": raw_bytes,
                "raw_reference": raw_reference,
            },
        )

    def _observation_join_reason(
        self: Any,
        *,
        source_ref: Mapping[str, Any],
        source_role: str,
        raw_hash: str,
        raw_snapshot_id: str,
        observation_id: Any,
        attempt_id: str,
        raw_reference: str,
    ) -> str | None:
        """Return a reason when the immutable observation join is invalid."""
        if not isinstance(observation_id, str) or not observation_id:
            return "observation_missing"
        if raw_snapshot_id != f"raw-{raw_hash}":
            return "raw_snapshot_identity_mismatch"
        expected_observation_id = _observation_id(
            raw_snapshot_id=raw_snapshot_id,
            raw_hash=raw_hash,
            source_ref=source_ref,
            role=source_role,
        )
        if observation_id != expected_observation_id:
            return "observation_identity_mismatch"
        try:
            observation = RawSnapshotStore(self.root).get_observation(observation_id)
        except (OSError, ValueError, CheckpointError):
            return "observation_missing"
        if observation.get("immutable") is not True:
            return "observation_not_immutable"
        observation_source = observation.get("source_ref")
        normalized_observation_source = _source_ref_payload(observation_source)
        if normalized_observation_source is None:
            return "observation_source_missing"
        if normalized_observation_source != dict(source_ref):
            return "observation_source_join_mismatch"
        if str(observation.get("raw_snapshot_id", "")) != raw_snapshot_id:
            return "observation_raw_join_mismatch"
        if str(observation.get("raw_sha256", "")) != raw_hash:
            return "observation_raw_join_mismatch"
        if str(observation.get("raw_content_hash", "")) != raw_hash:
            return "observation_raw_join_mismatch"
        if str(observation.get("role", "")) != source_role:
            return "observation_role_mismatch"
        if str(observation.get("observation_id", "")) != observation_id:
            return "observation_identity_mismatch"
        if str(observation.get("raw_reference", "")) != (
            f".aksantara/raw-snapshots/{raw_hash}.bin"
        ):
            return "observation_raw_join_mismatch"
        if not isinstance(attempt_id, str) or not attempt_id:
            return "observation_attempt_missing"
        if not isinstance(raw_reference, str) or not raw_reference:
            return "raw_join_missing"
        return None

    def candidate_evaluation(self: Any, run_id: str) -> dict[str, Any]:
        """Read a previously persisted candidate evaluation."""
        run_dir = self._existing_run_dir(run_id)
        return _read_json(run_dir / "candidate-evaluation.json")


def _candidate_header_reason(
    item: Mapping[str, Any],
    *,
    run_id: str,
    run_fingerprint: str,
    pins: Mapping[str, Any],
    report: Mapping[str, Any],
) -> tuple[str | None, str | None, str]:
    """Validate the outcome envelope shared by every candidate item."""
    stable_key = item.get("stable_key")
    if not isinstance(stable_key, str) or not stable_key:
        return "stable_key_missing", None, ""
    if item.get("run_id") != run_id:
        return "run_id_mismatch", stable_key, ""
    if item.get("attempt_run_id") != run_id:
        return "run_id_mismatch", stable_key, ""
    if item.get("catalog_fingerprint") != report.get("fingerprints", {}).get("catalog"):
        return "catalog_fingerprint_mismatch", stable_key, ""
    if item.get("run_fingerprint") != run_fingerprint:
        return "run_fingerprint_mismatch", stable_key, ""
    item_pins = item.get("pins")
    if not _valid_pins(item_pins):
        return "run_pins_missing", stable_key, ""
    pin_reason = _pin_mismatch_reason(item_pins, pins)
    if pin_reason is not None:
        return pin_reason, stable_key, ""
    if item.get("current") is not True or item.get("candidate_member") is not False:
        return "candidate_state_mismatch", stable_key, ""
    if str(item.get("outcome", "")) == "accepted" and (
        item.get("eligible") is not True
        or item.get("review_status") != "approved"
        or item.get("release_blocking") is not False
    ):
        return "review_required", stable_key, ""
    report_item = _find_by_key(report.get("outcomes"), stable_key)
    if report_item is None or dict(report_item) != dict(item):
        return "report_outcome_mismatch", stable_key, ""
    return None, stable_key, str(item.get("outcome", ""))


def _resolved_conflict(
    reviews: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the one review that explicitly selects the official side."""
    for review in reviews:
        if (
            review.get("type") == "lexical_conflict"
            and review.get("review_status") == "approved"
            and review.get("selected_authority") == "official"
        ):
            return review
    return None


def _blocking_review_reason(
    reviews: list[dict[str, Any]],
    resolved_conflict: Mapping[str, Any] | None,
) -> str | None:
    """Reject pending, quarantined, rejected, or release-blocking reviews."""
    for review in reviews:
        if review is resolved_conflict:
            continue
        if bool(review.get("release_blocking")) or review.get("review_status") in {
            "pending",
            "quarantined",
            "rejected",
        }:
            return "review_required"
    return None


def _official_source_reason(
    item: Mapping[str, Any],
    source_ref: Mapping[str, Any],
    pins: Mapping[str, Any],
) -> str | None:
    """Require an official SourceRef, role, and parser pin."""
    if source_ref["source_kind"] not in {"official-live", "official-snapshot"}:
        return "official_required"
    if (
        str(item.get("source_role", "")) != "official"
        or str(item.get("authority_role", "")) != "official"
    ):
        return "official_role_mismatch"
    if source_ref["parser_version"] != pins["parser_version"]:
        return "parser_pin_mismatch"
    return None


def _official_observation_reason(
    observation: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
    run_id: str,
    source_ref: Mapping[str, Any],
    attempt_id: str,
) -> str | None:
    """Ensure the logical outcome carries the same official observation join."""
    expected = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "source_ref": dict(source_ref),
        "source_role": "official",
        "raw_snapshot_id": item.get("raw_snapshot_id"),
        "observation_id": item.get("observation_id"),
        "raw_content_hash": item.get("raw_hash"),
        "canonical_content_hash": item.get("canonical_content_hash"),
    }
    for field, expected_value in expected.items():
        if observation.get(field) != expected_value:
            return "observation_attempt_mismatch"
    return None


def _inside_run(path: Path, root: Path, run_dir: Path) -> bool:
    """Return whether a resolved artifact path remains inside this run."""
    try:
        path.relative_to(root)
        path.relative_to(run_dir)
    except ValueError:
        return False
    return True


def _parsed_metadata_reason(
    parsed_payload: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
    run_id: str,
    run_fingerprint: str,
    catalog_fingerprint: str,
    pins: Mapping[str, Any],
    source_ref: Mapping[str, Any],
    stable_key: str,
    attempt_id: str,
) -> str | None:
    """Validate parsed artifact identity and its complete lineage envelope."""
    expected_hash = item.get("canonical_content_hash")
    if parsed_payload.get("run_id") != run_id:
        return "run_id_mismatch"
    if parsed_payload.get("stable_key") != stable_key:
        return "parsed_stable_key_mismatch"
    if parsed_payload.get("canonical_reference") != item.get("canonical_reference"):
        return "canonical_join_mismatch"
    if parsed_payload.get("canonical_content_hash") != expected_hash:
        return "canonical_hash_mismatch"
    if parsed_payload.get("fingerprints") != {
        "catalog": catalog_fingerprint,
        "run": run_fingerprint,
    }:
        return "run_fingerprint_mismatch"
    parsed_pins = parsed_payload.get("pins")
    if not _valid_pins(parsed_pins):
        return "run_pins_missing"
    pin_reason = _pin_mismatch_reason(parsed_pins, pins)
    if pin_reason is not None:
        return pin_reason
    serialization_reason = _canonical_serialization_reason(
        parsed_payload.get("canonical_serialization")
    )
    if serialization_reason is not None:
        return serialization_reason
    return _parsed_lineage_reason(
        parsed_payload.get("lineage"),
        run_id=run_id,
        stable_key=stable_key,
        run_fingerprint=run_fingerprint,
        catalog_fingerprint=catalog_fingerprint,
        pins=pins,
        source_ref=source_ref,
        item=item,
        attempt_id=attempt_id,
    )


def _parsed_entry_reason(
    entry: Any,
    *,
    item: Mapping[str, Any],
    source_ref: Mapping[str, Any],
    pins: Mapping[str, Any],
    expected_hash: str,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Validate canonical schema, source identity, and pinned transforms."""
    if not isinstance(entry, Mapping):
        return "parsed_entry_missing", None, None
    if set(entry) != set(CANONICAL_RECORD_FIELDS):
        return "parsed_entry_schema_mismatch", None, None
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        return "entry_id_missing", None, None
    if canonical_content_hash(dict(entry)) != expected_hash:
        return "canonical_hash_mismatch", None, None
    normalized_source = _source_ref_payload(entry.get("source"))
    if normalized_source is None:
        return "entry_source_missing", None, None
    if normalized_source != dict(source_ref):
        return "entry_source_join_mismatch", None, None
    if normalized_source["source_kind"] not in {
        "official-live",
        "official-snapshot",
    }:
        return "official_required", None, None
    if item.get("entry_id") != entry_id:
        return "entry_id_mismatch", None, None
    if entry.get("parser_version") != pins["parser_version"]:
        return "parser_pin_mismatch", None, None
    if entry.get("transform_version") != pins["transform_version"]:
        return "transform_pin_mismatch", None, None
    return None, normalized_source, entry_id


def _canonical_join_reason(
    root: Path,
    run_dir: Path,
    canonical_reference: str,
    expected_hash: str,
    entry: Mapping[str, Any],
) -> str | None:
    """Validate the immutable canonical artifact and exact serialization."""
    canonical_path = (root / canonical_reference).resolve()
    if not _inside_run(canonical_path, root, run_dir):
        return "canonical_join_outside_run"
    try:
        canonical_bytes = canonical_path.read_bytes()
    except (OSError, ValueError):
        return "canonical_join_missing"
    if content_hash_bytes(
        canonical_bytes
    ) != expected_hash or canonical_bytes != canonical_record_bytes(dict(entry)):
        return "canonical_hash_mismatch"
    if not canonical_bytes.endswith(b"\n"):
        return "canonical_serialization_mismatch"
    try:
        canonical_payload = json.loads(canonical_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "canonical_serialization_mismatch"
    if not isinstance(canonical_payload, Mapping):
        return "canonical_serialization_mismatch"
    if dict(canonical_payload) != canonical_record_payload(dict(entry)):
        return "canonical_serialization_mismatch"
    return None


def _reparse_raw_join(
    raw_bytes: bytes,
    source_ref: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> str | None:
    """Reparse raw bytes so stored parsed artifacts cannot be substituted."""
    try:
        from aksantara.domain.errors import AksantaraDomainError

        source_model = SourceRef.model_validate_strings(dict(source_ref))
        reparsed = parse_kbbi(raw_bytes, source_model)
        validate_entry(reparsed, raw_bytes=raw_bytes)
    except (ParserError, ValueError, TypeError, AksantaraDomainError):
        return "parsed_raw_join_mismatch"
    if reparsed.model_dump(mode="json") != dict(entry):
        return "parsed_raw_join_mismatch"
    return None


def _candidate_payload(
    *,
    item: Mapping[str, Any],
    entry: Mapping[str, Any],
    source_ref: Mapping[str, Any],
    run_fingerprint: str,
    catalog_fingerprint: str,
    pins: Mapping[str, Any],
    raw_hash: str,
    raw_snapshot_id: str,
    observation_id: Any,
    attempt_id: str,
    expected_hash: str,
    canonical_reference: str,
    parsed_reference: str,
    raw_reference: str,
    canonical_serialization: Any,
) -> dict[str, Any]:
    """Materialize the exact lineage envelope copied into a candidate."""
    return {
        "entry_id": entry["id"],
        "lema": entry.get("lema"),
        "entry": dict(entry),
        "run_fingerprint": run_fingerprint,
        "catalog_fingerprint": catalog_fingerprint,
        "pins": dict(pins),
        "source_ref": dict(source_ref),
        "source_role": "official",
        "authority_role": "official",
        "raw_sha256": raw_hash,
        "raw_content_hash": raw_hash,
        "raw_snapshot_id": raw_snapshot_id,
        "observation_id": observation_id,
        "attempt_id": attempt_id,
        "canonical_content_hash": expected_hash,
        "canonical_reference": canonical_reference,
        "parsed_reference": parsed_reference,
        "raw_reference": raw_reference,
        "canonical_serialization": canonical_serialization,
        "review_id": item.get("conflict_id") or item.get("review_id"),
    }


def _valid_hash(value: Any) -> bool:
    """Return whether a value is a lower-case SHA-256 digest."""
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _valid_pins(value: Any) -> bool:
    """Return whether all run policy pins are present and string-valued."""
    return (
        isinstance(value, Mapping)
        and _PIN_FIELDS.issubset(value)
        and all(
            isinstance(value[field], str) and bool(value[field].strip())
            for field in _PIN_FIELDS
        )
    )


def _pin_mismatch_reason(
    actual: Any,
    expected: Mapping[str, Any],
) -> str | None:
    """Return a stable error code for the first mismatched run pin."""
    if not isinstance(actual, Mapping):
        return "run_pins_missing"
    codes = {
        "parser_version": "parser_pin_mismatch",
        "transform_version": "transform_pin_mismatch",
        "validation_policy": "validation_policy_mismatch",
    }
    for field in sorted(_PIN_FIELDS):
        if actual.get(field) != expected.get(field):
            return codes[field]
    return None


def _matching_fingerprints(
    value: Any,
    *,
    catalog_fingerprint: str,
    run_fingerprint: str,
) -> bool:
    """Check the two release-relevant fingerprints without trusting callers."""
    return (
        isinstance(value, Mapping)
        and value.get("catalog") == catalog_fingerprint
        and value.get("run") == run_fingerprint
    )


def _matching_corpus(
    report_corpus: Any,
    preflight_catalog: Any,
    *,
    record_count: int,
) -> bool:
    """Compare the stable corpus identity while allowing layout metadata."""
    return (
        isinstance(report_corpus, Mapping)
        and isinstance(preflight_catalog, Mapping)
        and report_corpus.get("catalog_id") == preflight_catalog.get("id")
        and report_corpus.get("corpus_version")
        == preflight_catalog.get("corpus_version")
        and report_corpus.get("catalog_count") == record_count
    )


def _find_by_key(value: Any, stable_key: str) -> Mapping[str, Any] | None:
    """Find exactly one mapping keyed by stable_key in a durable list."""
    if not isinstance(value, list):
        return None
    matches = [
        item
        for item in value
        if isinstance(item, Mapping) and item.get("stable_key") == stable_key
    ]
    return matches[0] if len(matches) == 1 else None


def _source_ref_payload(value: Any) -> dict[str, Any] | None:
    """Validate and normalize the complete durable SourceRef payload."""
    if not isinstance(value, Mapping) or set(value) != _SOURCE_REF_FIELDS:
        return None
    try:
        # Durable JSON stores datetimes as ISO strings.  The model remains
        # strict for normal callers; model_validate_strings only parses the
        # already-published JSON representation back into that model.
        source_ref = SourceRef.model_validate_strings(dict(value))
    except (TypeError, ValueError):
        return None
    payload = source_ref.model_dump(mode="json")
    return dict(payload)


def _observation_id(
    *,
    raw_snapshot_id: str,
    raw_hash: str,
    source_ref: Mapping[str, Any],
    role: str,
) -> str:
    """Recompute the immutable observation identity from its full preimage."""
    preimage = {
        "raw_snapshot_id": raw_snapshot_id,
        "raw_sha256": raw_hash,
        "source_ref": dict(source_ref),
        "role": role,
    }
    return f"observation-{canonical_json_hash(preimage)[:32]}"


def _accepted_join_reason(
    report_join: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
    run_id: str,
    run_fingerprint: str,
    catalog_fingerprint: str,
    source_ref: Mapping[str, Any],
) -> str | None:
    """Check the report's release-scoped accepted join independently."""
    expected: dict[str, Any] = {
        "stable_key": item.get("stable_key"),
        "source_key": item.get("stable_key"),
        "entry_id": item.get("entry_id"),
        "run_id": run_id,
        "run_fingerprint": run_fingerprint,
        "catalog_fingerprint": catalog_fingerprint,
        "source_ref": dict(source_ref),
        "source_role": "official",
        "authority_role": "official",
        "pins": item.get("pins"),
        "raw_hash": item.get("raw_hash"),
        "raw_content_hash": item.get("raw_hash"),
        "canonical_hash": item.get("canonical_content_hash"),
        "canonical_content_hash": item.get("canonical_content_hash"),
        "raw_snapshot_id": item.get("raw_snapshot_id"),
        "observation_id": item.get("observation_id"),
        "attempt_id": item.get("attempt_id"),
        "canonical_reference": item.get("canonical_reference"),
        "parsed_reference": item.get("parsed_reference"),
        "eligible": True,
        "candidate_member": False,
    }
    for field, expected_value in expected.items():
        if report_join.get(field) != expected_value:
            return "accepted_join_mismatch"
    official = report_join.get("official_observation")
    if not isinstance(official, Mapping):
        return "accepted_join_mismatch"
    if (
        official.get("attempt_id") != item.get("attempt_id")
        or official.get("run_id") != run_id
        or official.get("source_ref") != dict(source_ref)
        or official.get("source_role") != "official"
        or official.get("raw_snapshot_id") != item.get("raw_snapshot_id")
        or official.get("observation_id") != item.get("observation_id")
        or official.get("raw_content_hash") != item.get("raw_hash")
        or official.get("canonical_content_hash") != item.get("canonical_content_hash")
    ):
        return "accepted_join_mismatch"
    return None


def _find_attempt(
    attempts_payload: Mapping[str, Any],
    *,
    attempt_id: str,
    stable_key: str,
    source_ref: Mapping[str, Any],
    item: Mapping[str, Any],
    run_id: str,
    catalog_fingerprint: str,
    run_fingerprint: str,
    pins: Mapping[str, Any],
) -> tuple[str | None, Mapping[str, Any] | None]:
    """Find and verify the one physical attempt that produced the outcome."""
    physical_attempts = attempts_payload.get("physical_attempts")
    if not isinstance(physical_attempts, list):
        return "observation_attempt_missing", None
    matches = [
        attempt
        for attempt in physical_attempts
        if isinstance(attempt, Mapping) and attempt.get("attempt_id") == attempt_id
    ]
    if len(matches) != 1:
        return "observation_attempt_missing", None
    attempt = matches[0]
    if (
        attempt.get("run_id") != run_id
        or attempt.get("catalog_fingerprint") != catalog_fingerprint
        or attempt.get("run_fingerprint") != run_fingerprint
        or attempt.get("stable_key") != stable_key
        or attempt.get("source_ref") != dict(source_ref)
        or attempt.get("source_kind") not in {"official-live", "official-snapshot"}
        or attempt.get("source_role") != "official"
        or attempt.get("authority_role") != "official"
        or attempt.get("outcome") != "accepted"
        or attempt.get("raw_hash") != item.get("raw_hash")
        or attempt.get("raw_content_hash") != item.get("raw_hash")
        or attempt.get("raw_snapshot_id") != item.get("raw_snapshot_id")
        or attempt.get("observation_id") != item.get("observation_id")
        or attempt.get("canonical_content_hash") != item.get("canonical_content_hash")
        or attempt.get("parsed_reference") != item.get("parsed_reference")
        or attempt.get("canonical_reference") != item.get("canonical_reference")
        or attempt.get("raw_reference") != item.get("raw_reference")
    ):
        return "observation_attempt_mismatch", None
    attempt_pins = attempt.get("pins")
    if not _valid_pins(attempt_pins):
        return "run_pins_missing", None
    pin_reason = _pin_mismatch_reason(attempt_pins, pins)
    if pin_reason is not None:
        return pin_reason, None
    nested = attempt.get("observation")
    if not isinstance(nested, Mapping):
        return "observation_attempt_mismatch", None
    if (
        nested.get("observation_id") != item.get("observation_id")
        or nested.get("raw_snapshot_id") != item.get("raw_snapshot_id")
        or nested.get("raw_sha256") != item.get("raw_hash")
        or nested.get("source_ref") != dict(source_ref)
        or nested.get("role") != "official"
    ):
        return "observation_attempt_mismatch", None
    return None, attempt


def _parsed_lineage_reason(
    lineage: Any,
    *,
    run_id: str,
    stable_key: str,
    run_fingerprint: str,
    catalog_fingerprint: str,
    pins: Mapping[str, Any],
    source_ref: Mapping[str, Any],
    item: Mapping[str, Any],
    attempt_id: str,
) -> str | None:
    """Check the parsed artifact's complete source/run lineage envelope."""
    if not isinstance(lineage, Mapping):
        return "parsed_lineage_missing"
    expected: dict[str, Any] = {
        "run_id": run_id,
        "stable_key": stable_key,
        "attempt_id": attempt_id,
        "catalog_fingerprint": catalog_fingerprint,
        "run_fingerprint": run_fingerprint,
        "source_ref": dict(source_ref),
        "source_role": "official",
        "authority_role": "official",
        "raw_hash": item.get("raw_hash"),
        "raw_content_hash": item.get("raw_hash"),
        "raw_snapshot_id": item.get("raw_snapshot_id"),
        "observation_id": item.get("observation_id"),
        "raw_reference": item.get("raw_reference"),
        "canonical_reference": item.get("canonical_reference"),
        "canonical_content_hash": item.get("canonical_content_hash"),
        "entry_id": item.get("entry_id"),
    }
    for field, expected_value in expected.items():
        if lineage.get(field) != expected_value:
            if field == "run_id":
                return "run_id_mismatch"
            if field in {"catalog_fingerprint", "run_fingerprint"}:
                return "run_fingerprint_mismatch"
            if field in {"source_role", "authority_role"}:
                return "official_role_mismatch"
            if field == "source_ref":
                return "parsed_source_join_mismatch"
            if field in {
                "raw_hash",
                "raw_content_hash",
                "raw_snapshot_id",
                "raw_reference",
            }:
                return "raw_join_mismatch"
            if field in {"observation_id", "attempt_id"}:
                return "observation_attempt_mismatch"
            if field == "canonical_reference":
                return "canonical_join_mismatch"
            return "canonical_hash_mismatch"
    lineage_pins = lineage.get("pins")
    if not _valid_pins(lineage_pins):
        return "run_pins_missing"
    pin_reason = _pin_mismatch_reason(lineage_pins, pins)
    if pin_reason is not None:
        return pin_reason
    return None


def _canonical_serialization_reason(value: Any) -> str | None:
    """Require the published canonical-record serialization contract."""
    expected = {
        "algorithm": "canonical-record-v1",
        "fields": list(CANONICAL_RECORD_FIELDS),
        "encoding": "UTF-8",
        "separators": [",", ":"],
        "sort_keys": True,
        "final_newline": True,
    }
    if value != expected:
        return "canonical_serialization_mismatch"
    return None


def _logical_attempt_reason(
    attempts_payload: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
    stable_key: str,
    run_id: str,
    run_fingerprint: str,
    pins: Mapping[str, Any],
    attempt_id: str,
) -> str | None:
    """Ensure the physical attempt is also linked through logical history."""
    logical_attempts = attempts_payload.get("attempts")
    if not isinstance(logical_attempts, list):
        return "observation_attempt_missing"
    matches = [
        value
        for value in logical_attempts
        if isinstance(value, Mapping) and value.get("stable_key") == stable_key
    ]
    if len(matches) != 1:
        return "observation_attempt_missing"
    logical = matches[0]
    if (
        logical.get("run_id") != run_id
        or logical.get("run_fingerprint") != run_fingerprint
    ):
        return "observation_attempt_mismatch"
    logical_pins = logical.get("pins")
    if not _valid_pins(logical_pins):
        return "run_pins_missing"
    pin_reason = _pin_mismatch_reason(logical_pins, pins)
    if pin_reason is not None:
        return pin_reason
    source_attempts = logical.get("source_attempts")
    if not isinstance(source_attempts, list):
        return "observation_attempt_missing"
    physical_matches = [
        value
        for value in source_attempts
        if isinstance(value, Mapping) and value.get("attempt_id") == attempt_id
    ]
    if len(physical_matches) != 1:
        return "observation_attempt_missing"
    if logical.get("attempt_count") not in {None, item.get("attempt_count")}:
        return "observation_attempt_mismatch"
    if logical.get("physical_attempt_count") not in {
        None,
        item.get("physical_attempt_count", item.get("attempt_count")),
    }:
        return "observation_attempt_mismatch"
    return None


def _resolved_review_join_reason(
    review: Mapping[str, Any],
    *,
    run_id: str,
    run_fingerprint: str,
    catalog_fingerprint: str,
    pins: Mapping[str, Any],
    stable_key: str,
    source_ref: Mapping[str, Any],
    raw_hash: str,
    raw_snapshot_id: str,
    observation_id: Any,
    canonical_hash: str,
    entry_id: str,
    parsed_reference: str,
    canonical_reference: str,
) -> str | None:
    """Ensure an approved conflict still points at the selected official side."""
    official = review.get("official")
    if not isinstance(official, Mapping):
        return "review_source_missing"
    expected = {
        "entry_id": entry_id,
        "stable_key": stable_key,
        "run_id": run_id,
        "catalog_fingerprint": catalog_fingerprint,
        "run_fingerprint": run_fingerprint,
        "pins": dict(pins),
        "source_ref": dict(source_ref),
        "source_kind": source_ref.get("source_kind"),
        "source_role": "official",
        "raw_sha256": raw_hash,
        "raw_content_hash": raw_hash,
        "raw_snapshot_id": raw_snapshot_id,
        "observation_id": observation_id,
        "parsed_reference": parsed_reference,
        "canonical_reference": canonical_reference,
        "canonical_content_hash": canonical_hash,
    }
    for field, expected_value in expected.items():
        if official.get(field) != expected_value:
            return "review_source_join_mismatch"
    if review.get("selected_authority") != "official":
        return "review_required"
    return None
