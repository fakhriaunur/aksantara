"""Single-observation transport, snapshot, parse, and validation lineage."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksantara.domain.authority import DEFAULT_VALIDATION_POLICY, ValidationPolicy
from aksantara.domain.errors import QuarantinedError, ValidationError
from aksantara.domain.provenance import canonical_content_hash, content_hash_bytes
from aksantara.ingest.checkpoint_authority import _source_identity
from aksantara.ingest.checkpoint_catalog import normalize_stable_key
from aksantara.ingest.checkpoint_storage import _safe_relative, _write_immutable
from aksantara.ingest.checkpoint_types import CatalogValidationError, _CatalogRecord
from aksantara.ingest.snapshots import RawSnapshotStore
from aksantara.parse.parser_contract import ParserError, parse_kbbi
from aksantara.validate.schema import validate_entry


def observe_binding(
    self: Any,
    record: _CatalogRecord,
    *,
    binding: Mapping[str, Any],
    run_dir: Path,
    selected_index: int,
    binding_index: int,
) -> dict[str, Any]:
    """Read and validate exactly one physical source observation.

    The returned object deliberately contains both the public attempt
    record and the private parsed entry.  Keeping those values together
    prevents a successful observation from being persisted without a
    joinable source/provenance/hash result.  The caller decides whether a
    valid entry is the selected official source or evidence only.
    """
    source_ref = binding["source_ref"]
    transport = binding["transport"]
    role = str(binding.get("role", "evidence"))
    source_kind = str(source_ref.source_kind)
    is_official = source_kind in {"official-live", "official-snapshot"}
    if is_official:
        role = "official"
    run_id = run_dir.name
    attempt_id = f"attempt-{run_id}-{selected_index + 1:04d}-{binding_index + 1:03d}"
    attempt: dict[str, Any] = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "stable_key": record.stable_key,
        "selected_index": selected_index,
        "binding_index": binding_index,
        "attempt": binding_index + 1,
        "transport_attempt": 1,
        "validation_attempt": 0,
        "adapter": transport["adapter"],
        "status": transport["status"],
        "source_kind": source_kind,
        "source_role": role,
        "role": role,
        "authority_role": role,
        "source_ref": _source_identity(source_ref),
        "retry_decision": False,
        "outcome": "pending",
        "transport_result": "not_attempted",
        "parse_result": "not_attempted",
        "validation_result": "not_attempted",
        "conflict_result": "not_evaluated",
        "raw_hash": None,
        "raw_content_hash": None,
        "raw_snapshot_id": None,
        "observation_id": None,
        "raw_reference": None,
        "canonical_content_hash": None,
        "conflict_id": None,
        "error": None,
    }
    result: dict[str, Any] = {
        "attempt": attempt,
        "source_ref": source_ref,
        "role": role,
        "source_kind": source_kind,
        "is_official": is_official,
        "entry": None,
        "observation": {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "stable_key": record.stable_key,
            "source_ref": _source_identity(source_ref),
            "source_kind": source_kind,
            "source_role": role,
            "role": role,
            "authority_role": role,
            "raw_sha256": None,
            "raw_content_hash": None,
            "raw_snapshot_id": None,
            "observation_id": None,
            "raw_reference": None,
            "canonical_content_hash": None,
            "entry_id": None,
            "lema": None,
            "conflict_id": None,
            "conflict_result": "not_evaluated",
        },
    }

    def finish(
        *,
        outcome: str,
        transport_result: str | None = None,
        parse_result: str | None = None,
        validation_result: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt["outcome"] = outcome
        if transport_result is not None:
            attempt["transport_result"] = transport_result
        if parse_result is not None:
            attempt["parse_result"] = parse_result
        if validation_result is not None:
            attempt["validation_result"] = validation_result
        if error is not None:
            attempt["error"] = error
        for key in (
            "raw_hash",
            "raw_content_hash",
            "raw_snapshot_id",
            "observation_id",
            "raw_reference",
            "canonical_content_hash",
            "conflict_id",
        ):
            result["observation"][key] = attempt.get(key)
        result["observation"]["outcome"] = attempt["outcome"]
        result["observation"]["parse_result"] = attempt["parse_result"]
        result["observation"]["validation_result"] = attempt["validation_result"]
        result["observation"]["transport_result"] = attempt["transport_result"]
        result["observation"]["error"] = attempt["error"]
        result["observation"]["conflict_result"] = attempt["conflict_result"]
        return result

    status = int(transport["status"])
    if status >= 400:
        retryable = status == 429 or status >= 500
        attempt["retry_decision"] = retryable
        return finish(
            outcome="retryable" if retryable else "failed",
            transport_result="retryable" if retryable else "permanent_failure",
            error={
                "code": "transport_retryable" if retryable else "transport_permanent",
                "message": f"fixture transport status {status}",
            },
        )

    try:
        raw = self._fixture_bytes_for_transport(transport)
    except (CatalogValidationError, OSError) as exc:
        return finish(
            outcome="rejected",
            transport_result="read_error",
            error={
                "code": "fixture_read_error",
                "message": str(exc),
            },
        )

    actual_hash = content_hash_bytes(raw)
    attempt["raw_hash"] = actual_hash
    attempt["raw_content_hash"] = actual_hash
    raw_path = run_dir / "raw" / f"{actual_hash}.bin"
    _write_immutable(raw_path, raw, self.root)
    attempt["raw_reference"] = _safe_relative(self.root, raw_path)
    result["observation"]["raw_reference"] = attempt["raw_reference"]
    result["observation"]["raw_sha256"] = actual_hash
    result["observation"]["raw_content_hash"] = actual_hash
    expected_hash = str(transport["expected_raw_hash"])
    if not expected_hash or actual_hash != expected_hash:
        return finish(
            outcome="rejected",
            transport_result="hash_mismatch",
            error={
                "code": "raw_hash_mismatch",
                "message": (
                    f"raw hash mismatch: expected {expected_hash or '<missing>'} "
                    f"actual {actual_hash}"
                ),
                "expected": expected_hash,
                "actual": actual_hash,
            },
        )

    raw_store = RawSnapshotStore(self.root)
    try:
        raw_observation = raw_store.put(
            raw,
            source_ref,
            expected_raw_hash=expected_hash,
            role=role,
        )
    except (OSError, ValueError) as exc:
        return finish(
            outcome="failed",
            transport_result="success",
            error={
                "code": "raw_snapshot_persistence_failure",
                "message": str(exc),
            },
        )
    attempt["raw_snapshot_id"] = raw_observation["raw_snapshot_id"]
    attempt["observation_id"] = raw_observation["observation_id"]
    result["observation"]["raw_snapshot_id"] = attempt["raw_snapshot_id"]
    result["observation"]["observation_id"] = attempt["observation_id"]
    attempt["transport_result"] = "success"

    try:
        entry = parse_kbbi(raw, source_ref)
    except (ParserError, ValueError, TypeError) as exc:
        return finish(
            outcome="rejected",
            parse_result="failed",
            error={
                "code": "parse_failure",
                "message": str(exc),
            },
        )
    attempt["parse_result"] = "success"
    attempt["validation_attempt"] = 1
    policy = (
        DEFAULT_VALIDATION_POLICY
        if is_official
        else ValidationPolicy(require_official_source_for_canonical=False)
    )
    try:
        validate_entry(entry, raw_bytes=raw, policy=policy)
        if (
            normalize_stable_key(entry.id) != record.stable_key
            and normalize_stable_key(entry.lema) != record.stable_key
        ):
            raise ValidationError("parsed entry identity does not match stable_key")
    except QuarantinedError as exc:
        return finish(
            outcome="quarantined",
            validation_result="failed",
            error={
                "code": exc.reason,
                "message": str(exc),
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        return finish(
            outcome="rejected",
            validation_result="failed",
            error={
                "code": "deterministic_validation_failure",
                "message": str(exc),
            },
        )

    canonical_hash = canonical_content_hash(entry)
    attempt["validation_result"] = "success"
    attempt["canonical_content_hash"] = canonical_hash
    result["entry"] = entry
    result["observation"].update(
        {
            "canonical_content_hash": canonical_hash,
            "entry_id": entry.id,
            "lema": entry.lema,
        }
    )
    return finish(
        outcome="accepted",
        transport_result="success",
        parse_result="success",
        validation_result="success",
    )


root: Path


__all__ = ["observe_binding"]
