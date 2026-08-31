"""Fail-closed candidate evaluation for checkpoint runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksantara.domain.provenance import canonical_content_hash, content_hash_bytes
from aksantara.ingest.checkpoint_storage import (
    _hash_payload,
    _read_json,
    _safe_relative,
    _write_immutable,
    _write_state_json,
)
from aksantara.ingest.checkpoint_types import (
    CheckpointError,
    CheckpointPersistenceError,
)
from aksantara.ingest.snapshots import RawSnapshotStore
from aksantara.validate.review import ReviewStore


class CheckpointCandidateMixin:
    """Evaluate exact joins without mutating canonical or release state."""

    root: Path

    def evaluate_candidate(
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

        run_dir = self._existing_run_dir(run_id)
        report = _read_json(run_dir / "report.json")
        checkpoint = _read_json(run_dir / "checkpoint.json")
        outcomes_payload = _read_json(run_dir / "outcomes.json")
        outcomes = outcomes_payload.get("outcomes", [])
        if not isinstance(outcomes, list):
            raise CheckpointPersistenceError(
                "outcomes artifact does not contain an array",
                details={"run_id": run_id},
            )
        preflight = _read_json(run_dir / "preflight.json")
        run_fingerprint = str(
            preflight.get("fingerprints", {}).get(
                "run", report.get("fingerprints", {}).get("run", "")
            )
        )
        if not run_fingerprint:
            raise CheckpointPersistenceError(
                "run fingerprint is missing from durable checkpoint state",
                details={"run_id": run_id},
            )

        reviews = [
            review
            for review in ReviewStore(root=self.root).list_all()
            if str(review.get("first_seen_run", "")) == run_id
        ]
        review_by_key: dict[str, list[dict[str, Any]]] = {}
        for review in reviews:
            key = str(review.get("stable_key", ""))
            if key:
                review_by_key.setdefault(key, []).append(review)

        eligible_items: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for item in sorted(
            (value for value in outcomes if isinstance(value, Mapping)),
            key=lambda value: (
                str(value.get("stable_key", "")),
                int(value.get("selected_index", 0)),
            ),
        ):
            stable_key = str(item.get("stable_key", ""))
            item_reviews = review_by_key.get(stable_key, [])
            item_eligible, reason, candidate_item = self._candidate_item(
                item,
                item_reviews,
                run_dir=run_dir,
                run_fingerprint=run_fingerprint,
            )
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

        checkpoint_complete = bool(
            report.get("completion", {}).get("checkpoint_complete")
            and report.get("status") == "completed"
            and len(outcomes) == int(checkpoint.get("selected_count", len(outcomes)))
            and all(
                isinstance(item, Mapping)
                and item.get("outcome")
                in {"accepted", "quarantined", "rejected", "failed"}
                for item in outcomes
            )
        )
        reasons: list[str] = []
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

        candidate: dict[str, Any] | None = None
        if eligible:
            candidate_payload: dict[str, Any] = {
                "schema_version": "checkpoint-candidate-v1",
                "candidate_id": f"candidate-{run_fingerprint}",
                "run_id": run_id,
                "run_fingerprint": run_fingerprint,
                "catalog_fingerprint": report.get("fingerprints", {}).get("catalog"),
                "corpus": report.get("corpus", {}),
                "selection": report.get("selection", {}),
                "pins": report.get("pins", {}),
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
            candidate_path = (
                self.root
                / ".aksantara"
                / "candidates"
                / f"{candidate_payload['candidate_id']}.json"
            )
            _write_immutable(
                candidate_path,
                json.dumps(
                    candidate_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n",
                self.root,
            )
            candidate = {
                **candidate_payload,
                "reference": _safe_relative(self.root, candidate_path),
                "candidate_created": True,
            }

        evaluation = {
            "schema_version": "checkpoint-candidate-evaluation-v1",
            "run_id": run_id,
            "run_fingerprint": run_fingerprint,
            "candidate_created": candidate is not None,
            "candidate": candidate,
            "eligible": eligible,
            "checkpoint_complete": checkpoint_complete,
            "release_approval": {
                "approved": release_approved,
                "reviewer": (release_reviewer or "").strip(),
                "reason": (release_reason or "").strip(),
            },
            "eligible_count": len(eligible_items),
            "selected_count": len(outcomes),
            "excluded": excluded,
            "reason_codes": reasons,
            "current_version_changed": False,
            "vector_work": False,
        }
        evaluation_path = run_dir / "candidate-evaluation.json"
        _write_state_json(evaluation_path, evaluation, self.root)
        return evaluation

    def _candidate_item(
        self: Any,
        item: Mapping[str, Any],
        reviews: list[dict[str, Any]],
        *,
        run_dir: Path,
        run_fingerprint: str,
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        """Validate one outcome and materialize its exact candidate join."""
        outcome = str(item.get("outcome", ""))
        resolved_conflict = None
        for review in reviews:
            if (
                review.get("type") == "lexical_conflict"
                and review.get("review_status") == "approved"
                and review.get("selected_authority") == "official"
            ):
                resolved_conflict = review
                break
        if outcome != "accepted" and resolved_conflict is None:
            return (
                False,
                str(item.get("exclusion_reason", outcome or "not_eligible")),
                None,
            )
        for review in reviews:
            if review is resolved_conflict:
                continue
            if bool(review.get("release_blocking")) or review.get("review_status") in {
                "pending",
                "quarantined",
                "rejected",
            }:
                return False, "review_required", None

        source_ref = item.get("source_ref")
        if not isinstance(source_ref, Mapping):
            return False, "source_ref_missing", None
        source_kind = str(source_ref.get("source_kind", ""))
        if source_kind not in {"official-live", "official-snapshot"}:
            return False, "official_required", None

        parsed_reference = item.get("parsed_reference")
        if not isinstance(parsed_reference, str) or not parsed_reference:
            return False, "parsed_join_missing", None
        parsed_path = (self.root / parsed_reference).resolve()
        try:
            parsed_path.relative_to(self.root)
            parsed_path.relative_to(run_dir)
        except ValueError:
            return False, "parsed_join_outside_run", None
        try:
            parsed_payload = _read_json(parsed_path)
        except CheckpointError:
            return False, "parsed_join_missing", None
        entry = parsed_payload.get("entry")
        if not isinstance(entry, Mapping):
            return False, "parsed_entry_missing", None
        entry_id = str(entry.get("id", ""))
        if not entry_id:
            return False, "entry_id_missing", None
        expected_hash = str(item.get("canonical_content_hash", ""))
        actual_hash = canonical_content_hash(dict(entry))
        if not expected_hash or actual_hash != expected_hash:
            return False, "canonical_hash_mismatch", None
        entry_source = entry.get("source")
        if not isinstance(entry_source, Mapping):
            return False, "entry_source_missing", None
        if str(entry_source.get("source_kind", "")) not in {
            "official-live",
            "official-snapshot",
        }:
            return False, "official_required", None
        if str(item.get("entry_id", entry_id)) != entry_id:
            return False, "entry_id_mismatch", None
        if (
            str(parsed_payload.get("canonical_content_hash", expected_hash))
            != expected_hash
        ):
            return False, "canonical_hash_mismatch", None

        raw_hash = str(item.get("raw_hash", ""))
        if len(raw_hash) != 64:
            return False, "raw_hash_missing", None
        raw_snapshot_id = item.get("raw_snapshot_id")
        if not isinstance(raw_snapshot_id, str) or not raw_snapshot_id:
            return False, "raw_snapshot_missing", None
        try:
            raw_bytes = RawSnapshotStore(self.root).get(raw_snapshot_id)
        except (OSError, ValueError, FileNotFoundError):
            return False, "raw_snapshot_missing", None
        if content_hash_bytes(raw_bytes) != raw_hash:
            return False, "raw_hash_mismatch", None
        if str(source_ref.get("content_hash", "")) != raw_hash:
            return False, "raw_source_join_mismatch", None
        if str(entry_source.get("content_hash", "")) != raw_hash:
            return False, "raw_entry_join_mismatch", None
        return (
            True,
            None,
            {
                "entry_id": entry_id,
                "lema": entry.get("lema"),
                "entry": dict(entry),
                "run_fingerprint": run_fingerprint,
                "source_ref": dict(source_ref),
                "raw_sha256": raw_hash,
                "raw_snapshot_id": raw_snapshot_id,
                "observation_id": item.get("observation_id"),
                "canonical_content_hash": expected_hash,
                "review_id": item.get("conflict_id") or item.get("review_id"),
            },
        )

    def candidate_evaluation(self: Any, run_id: str) -> dict[str, Any]:
        """Read a previously persisted candidate evaluation."""
        run_dir = self._existing_run_dir(run_id)
        return _read_json(run_dir / "candidate-evaluation.json")
