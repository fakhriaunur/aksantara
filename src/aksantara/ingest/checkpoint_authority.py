"""Authority observation processing for checkpoint execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aksantara.domain.authority import DEFAULT_VALIDATION_POLICY
from aksantara.domain.errors import QuarantinedError, ValidationError
from aksantara.domain.provenance import canonical_content_hash, content_hash_bytes
from aksantara.ingest.checkpoint_storage import (
    _safe_relative,
    _write_immutable,
)
from aksantara.ingest.checkpoint_types import (
    AUTHORITY_POLICY_VERSION,
    CatalogValidationError,
    _CatalogRecord,
)
from aksantara.ingest.snapshots import RawSnapshotStore
from aksantara.parse.parser_contract import ParserError, parse_kbbi
from aksantara.validate.conflicts import lexical_field_diffs
from aksantara.validate.review import ReviewStore
from aksantara.validate.schema import validate_entry


def _source_identity(source_ref: Any) -> dict[str, Any]:
    """Return the stable and volatile provenance fields for an observation."""
    payload = source_ref.model_dump(mode="json")
    return {
        "url": payload["url"],
        "source_kind": payload["source_kind"],
        "edition": payload["edition"],
        "source_version": payload["source_version"],
        "retrieved_at": payload["retrieved_at"],
        "content_hash": payload["content_hash"],
        "parser_version": payload["parser_version"],
    }


class CheckpointAuthorityMixin:
    """Process ordered official/evidence observations and review records."""

    root: Path

    def _process_without_official(
        self: Any,
        record: _CatalogRecord,
        *,
        bindings: list[dict[str, Any]],
        run_dir: Path,
        selected_index: int,
        reason: str,
        initial_attempt: dict[str, Any],
        final_outcome: str = "quarantined",
        final_exclusion_reason: str = "official_required",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Record evidence when no verified official result is canonical.

        This path is deliberately terminal.  It may preserve and parse a
        fallback observation for review, but it never writes a parsed
        canonical artifact and never returns an accepted/candidate row.
        """
        review_store = ReviewStore(root=self.root)
        raw_store = RawSnapshotStore(self.root)
        observations: list[dict[str, Any]] = []
        source_attempts: list[dict[str, Any]] = []
        review_id: str | None = None
        entry_id = record.stable_key

        for binding_index, binding in enumerate(bindings):
            source_ref = binding["source_ref"]
            transport = binding["transport"]
            role = str(binding.get("role", "evidence"))
            attempt: dict[str, Any] = {
                "stable_key": record.stable_key,
                "attempt": binding_index + 1,
                "binding_index": binding_index,
                "adapter": transport["adapter"],
                "status": transport["status"],
                "source_kind": source_ref.source_kind,
                "source_role": role,
                "outcome": "pending",
                "retry_decision": False,
                "validation_attempt": 0,
            }
            source: dict[str, Any] = {
                "source_ref": source_ref.model_dump(mode="json"),
                "source_kind": source_ref.source_kind,
                "source_role": role,
                "raw_sha256": None,
                "observation_id": None,
            }
            if transport["status"] >= 400:
                attempt["outcome"] = (
                    "retryable"
                    if transport["status"] == 429 or transport["status"] >= 500
                    else "failed"
                )
                attempt["retry_decision"] = attempt["outcome"] == "retryable"
                attempt["error"] = {
                    "code": "transport_retryable"
                    if attempt["outcome"] == "retryable"
                    else "transport_permanent",
                    "message": f"fixture transport status {transport['status']}",
                }
                source_attempts.append(attempt)
                observations.append(
                    {
                        **source,
                        "role": role,
                        "review_id": None,
                    }
                )
                continue
            try:
                raw = self._fixture_bytes_for_transport(transport)
            except (CatalogValidationError, OSError) as exc:
                attempt["outcome"] = "rejected"
                attempt["error"] = {
                    "code": "fixture_read_error",
                    "message": str(exc),
                }
                quarantine = review_store.persist_quarantine(
                    entry_id=entry_id,
                    stable_key=record.stable_key,
                    reason="fixture_read_error",
                    source=source,
                    first_seen_run=self._current_run_id(run_dir),
                    policy_version=AUTHORITY_POLICY_VERSION,
                    details=str(exc),
                )
                review_id = review_id or quarantine["review_id"]
                observations.append(
                    {**source, "role": role, "review_id": quarantine["review_id"]}
                )
                source_attempts.append(attempt)
                continue

            actual_hash = content_hash_bytes(raw)
            source["raw_sha256"] = actual_hash
            expected_hash = str(transport["expected_raw_hash"])
            raw_path = run_dir / "raw" / f"{actual_hash}.bin"
            _write_immutable(raw_path, raw, self.root)
            source["raw_reference"] = _safe_relative(self.root, raw_path)
            if not expected_hash or actual_hash != expected_hash:
                attempt["outcome"] = "rejected"
                attempt["error"] = {
                    "code": "raw_hash_mismatch",
                    "message": (
                        f"raw hash mismatch: expected {expected_hash or '<missing>'} "
                        f"actual {actual_hash}"
                    ),
                    "expected": expected_hash,
                    "actual": actual_hash,
                }
                quarantine = review_store.persist_quarantine(
                    entry_id=entry_id,
                    stable_key=record.stable_key,
                    reason="raw_hash_mismatch",
                    source=source,
                    first_seen_run=self._current_run_id(run_dir),
                    policy_version=AUTHORITY_POLICY_VERSION,
                    details=attempt["error"]["message"],
                )
                review_id = review_id or quarantine["review_id"]
                observations.append(
                    {**source, "role": role, "review_id": quarantine["review_id"]}
                )
                source_attempts.append(attempt)
                continue
            try:
                raw_observation = raw_store.put(
                    raw,
                    source_ref,
                    expected_raw_hash=expected_hash,
                    role=role,
                )
                source.update(
                    {
                        "raw_snapshot_id": raw_observation["raw_snapshot_id"],
                        "observation_id": raw_observation["observation_id"],
                    }
                )
                entry = parse_kbbi(raw, source_ref)
                validate_entry(
                    entry,
                    raw_bytes=raw,
                    policy=type(DEFAULT_VALIDATION_POLICY)(
                        require_official_source_for_canonical=False
                    ),
                )
                source.update(
                    {
                        "entry_id": entry.id,
                        "lema": entry.lema,
                        "canonical_content_hash": canonical_content_hash(entry),
                        "entry": entry.model_dump(mode="json"),
                    }
                )
                entry_id = entry.id
                attempt["validation_attempt"] = 1
                attempt["outcome"] = "quarantined"
                attempt["error"] = {
                    "code": "official_required",
                    "message": "valid lower-authority evidence cannot become canonical",
                }
            except (
                ParserError,
                QuarantinedError,
                ValidationError,
                ValueError,
                TypeError,
            ) as exc:
                attempt["outcome"] = "rejected"
                attempt["error"] = {
                    "code": "fallback_validation_failure",
                    "message": str(exc),
                }
            source_attempts.append(attempt)
            if attempt["outcome"] == "rejected":
                quarantine_reason = "fallback_validation_failure"
            else:
                quarantine_reason = reason
            quarantine = review_store.persist_quarantine(
                entry_id=entry_id,
                stable_key=record.stable_key,
                reason=quarantine_reason,
                source=source,
                first_seen_run=self._current_run_id(run_dir),
                policy_version=AUTHORITY_POLICY_VERSION,
                details=str(attempt.get("error", {}).get("message", reason)),
            )
            review_id = review_id or quarantine["review_id"]
            observations.append(
                {**source, "role": role, "review_id": quarantine["review_id"]}
            )

        if review_id is None:
            primary_source = {
                "source_ref": bindings[0]["source_ref"].model_dump(mode="json"),
                "source_kind": bindings[0]["source_ref"].source_kind,
                "source_role": str(bindings[0].get("role", "evidence")),
                "raw_sha256": None,
                "observation_id": None,
            }
            quarantine = review_store.persist_quarantine(
                entry_id=entry_id,
                stable_key=record.stable_key,
                reason=reason,
                source=primary_source,
                first_seen_run=self._current_run_id(run_dir),
                policy_version=AUTHORITY_POLICY_VERSION,
                details="no adapter-verified official observation succeeded",
            )
            review_id = quarantine["review_id"]

        initial_attempt.update(
            {
                "outcome": final_outcome,
                "error": {
                    "code": final_exclusion_reason,
                    "message": (
                        "no adapter-verified official observation succeeded"
                        if final_outcome == "quarantined"
                        else "official transport outcome requires retry"
                    ),
                    "review_id": review_id,
                },
                "source_attempts": source_attempts,
                "attempt_count": len(source_attempts),
                "source_order": [
                    {
                        "source_kind": value["source_ref"].source_kind,
                        "role": str(value.get("role", "evidence")),
                    }
                    for value in bindings
                ],
            }
        )
        primary_ref = bindings[0]["source_ref"]
        outcome = {
            "stable_key": record.stable_key,
            "selected_index": selected_index,
            "outcome": final_outcome,
            "reason": reason,
            "exclusion_reason": final_exclusion_reason,
            "attempt_count": len(source_attempts),
            "source_ref": _source_identity(primary_ref),
            "candidate_namespace": False,
            "observations": observations,
            "review_id": review_id,
            "review_status": "quarantined",
            "release_blocking": True,
        }
        return outcome, initial_attempt

    @staticmethod
    def _ordered_bindings(record: _CatalogRecord) -> list[dict[str, Any]]:
        """Put an adapter-verified official binding before all evidence."""
        primary = {
            "role": "official"
            if record.source_ref.source_kind in {"official-live", "official-snapshot"}
            else "evidence",
            "source_ref": record.source_ref,
            "transport": record.transport,
        }
        values = [primary, *record.observations]
        normalized: list[dict[str, Any]] = []
        for value_index, value in enumerate(values):
            source_kind = value["source_ref"].source_kind
            role = (
                "official"
                if source_kind in {"official-live", "official-snapshot"}
                else str(value.get("role", "evidence"))
            )
            normalized.append(
                {
                    **value,
                    "role": role,
                    "_primary_binding": value_index == 0,
                }
            )
        values = normalized
        values.sort(
            key=lambda value: (
                0
                if value["source_ref"].source_kind
                in {"official-live", "official-snapshot"}
                else 1,
                0 if bool(value.get("_primary_binding")) else 1,
                str(value["source_ref"].url),
                str(value["source_ref"].content_hash),
            )
        )
        return values

    def _process_additional_observations(
        self: Any,
        record: _CatalogRecord,
        *,
        run_dir: Path,
        selected_index: int,
        official_entry: Any,
        canonical_hash: str,
        bindings: list[dict[str, Any]] | None = None,
        official_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Parse fallback evidence after the official observation."""
        observations: list[dict[str, Any]] = []
        conflict: dict[str, Any] | None = None
        extra_bindings = (
            list(bindings)
            if bindings is not None
            else [
                {
                    "role": observation.get("role", "evidence"),
                    "source_ref": observation["source_ref"],
                    "transport": observation["transport"],
                }
                for observation in record.observations
            ]
        )
        if not extra_bindings:
            return {"observations": observations, "conflict": conflict}
        review_store = ReviewStore(root=self.root)
        raw_store = RawSnapshotStore(self.root)
        for _observation_index, observation in enumerate(extra_bindings, start=1):
            source_ref = observation["source_ref"]
            transport = observation["transport"]
            source_kind = source_ref.source_kind
            role = str(observation.get("role", "evidence"))
            try:
                raw = self._fixture_bytes_for_transport(transport)
            except (CatalogValidationError, OSError) as exc:
                source = {
                    "source_ref": source_ref.model_dump(mode="json"),
                    "source_kind": source_kind,
                    "raw_sha256": None,
                    "observation_id": None,
                }
                quarantine = review_store.persist_quarantine(
                    entry_id=official_entry.id,
                    stable_key=record.stable_key,
                    reason="fixture_read_error",
                    source=source,
                    first_seen_run=self._current_run_id(run_dir),
                    policy_version=AUTHORITY_POLICY_VERSION,
                    details=str(exc),
                )
                observations.append(
                    {
                        "role": role,
                        "source_kind": source_kind,
                        "source_ref": source_ref.model_dump(mode="json"),
                        "raw_sha256": None,
                        "review_id": quarantine["review_id"],
                    }
                )
                continue
            actual_hash = content_hash_bytes(raw)
            expected_hash = str(transport["expected_raw_hash"])
            source = {
                "source_ref": source_ref.model_dump(mode="json"),
                "source_kind": source_kind,
                "raw_sha256": actual_hash,
            }
            raw_path = run_dir / "raw" / f"{actual_hash}.bin"
            _write_immutable(raw_path, raw, self.root)
            try:
                raw_observation = raw_store.put(
                    raw,
                    source_ref,
                    expected_raw_hash=expected_hash,
                    role=role,
                )
                source.update(
                    {
                        "raw_snapshot_id": raw_observation["raw_snapshot_id"],
                        "observation_id": raw_observation["observation_id"],
                        "raw_reference": _safe_relative(self.root, raw_path),
                    }
                )
            except (OSError, ValueError) as exc:
                quarantine = review_store.persist_quarantine(
                    entry_id=official_entry.id,
                    stable_key=record.stable_key,
                    reason="raw_hash_mismatch",
                    source=source,
                    first_seen_run=self._current_run_id(run_dir),
                    policy_version=AUTHORITY_POLICY_VERSION,
                    details=str(exc),
                )
                source["review_id"] = quarantine["review_id"]
                observations.append(
                    {
                        "role": role,
                        "source_kind": source_kind,
                        **source,
                    }
                )
                continue
            try:
                fallback_entry = parse_kbbi(raw, source_ref)
                validate_entry(
                    fallback_entry,
                    raw_bytes=raw,
                    policy=type(DEFAULT_VALIDATION_POLICY)(
                        require_official_source_for_canonical=False
                    ),
                )
            except Exception as exc:
                quarantine = review_store.persist_quarantine(
                    entry_id=official_entry.id,
                    stable_key=record.stable_key,
                    reason="fallback_validation_failure",
                    source=source,
                    first_seen_run=self._current_run_id(run_dir),
                    policy_version=AUTHORITY_POLICY_VERSION,
                    details=str(exc),
                )
                source["review_id"] = quarantine["review_id"]
                observations.append(
                    {
                        "role": role,
                        "source_kind": source_kind,
                        **source,
                    }
                )
                continue
            fallback_hash = canonical_content_hash(fallback_entry)
            field_diffs = lexical_field_diffs(official_entry, fallback_entry)
            observations.append(
                {
                    "role": role,
                    "source_kind": source_kind,
                    **source,
                    "canonical_content_hash": fallback_hash,
                    "differing_fields": [str(item["field"]) for item in field_diffs],
                }
            )
            if (
                field_diffs
                and conflict is None
                and source_kind not in {"official-live", "official-snapshot"}
            ):
                official_side = {
                    "source_ref": official_entry.source.model_dump(mode="json"),
                    "source_kind": official_entry.source.source_kind,
                    "raw_sha256": official_entry.source.content_hash,
                    "raw_snapshot_id": (official_observation or {}).get(
                        "raw_snapshot_id"
                    ),
                    "observation_id": (official_observation or {}).get(
                        "observation_id"
                    ),
                    "canonical_content_hash": canonical_hash,
                    "entry": official_entry.model_dump(mode="json"),
                }
                fallback_side = {
                    **source,
                    "canonical_content_hash": fallback_hash,
                    "entry": fallback_entry.model_dump(mode="json"),
                }
                conflict = review_store.persist_conflict(
                    entry_id=official_entry.id,
                    stable_key=record.stable_key,
                    official=official_side,
                    fallback=fallback_side,
                    differing_fields=[str(item["field"]) for item in field_diffs],
                    field_diffs=field_diffs,
                    first_seen_run=self._current_run_id(run_dir),
                    policy_version=AUTHORITY_POLICY_VERSION,
                )
            elif not field_diffs and source_kind not in {
                "official-live",
                "official-snapshot",
            }:
                # Equal lexical content from lower authority remains joined
                # evidence and does not block the official canonical row.
                pass
        return {"observations": observations, "conflict": conflict}

    @staticmethod
    def _current_run_id(run_dir: Path) -> str:
        return run_dir.name
