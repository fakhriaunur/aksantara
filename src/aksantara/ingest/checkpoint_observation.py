"""Single-observation transport, snapshot, parse, and validation lineage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from aksantara.domain.authority import DEFAULT_VALIDATION_POLICY, ValidationPolicy
from aksantara.domain.errors import QuarantinedError, ValidationError
from aksantara.domain.provenance import canonical_content_hash, content_hash_bytes
from aksantara.ingest.checkpoint_authority import _source_identity
from aksantara.ingest.checkpoint_catalog import normalize_stable_key
from aksantara.ingest.checkpoint_storage import _safe_relative, _write_immutable
from aksantara.ingest.checkpoint_types import CatalogValidationError, _CatalogRecord
from aksantara.ingest.rate_limit import (
    RetryConfig,
    calculate_backoff,
    is_retryable_status,
)
from aksantara.ingest.snapshots import RawSnapshotStore
from aksantara.parse.parser_contract import ParserError, parse_kbbi
from aksantara.validate.schema import validate_entry


def _parse_retry_after(value: Any) -> float | None:
    """Parse Retry-After header value per contract: integer seconds or http-date.

    Invalid, negative, or unparsable values return None and backoff is used.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # Try integer seconds
    try:
        # Retry-After can be quoted or plain
        seconds = float(raw)
        if seconds < 0 or seconds != seconds:  # NaN check
            return None
        return seconds
    except ValueError:
        pass
    # Try HTTP date
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = (dt.astimezone(UTC) - now).total_seconds()
        if delta < 0:
            return 0.0
        return delta
    except Exception:
        return None


def observe_binding(  # noqa: C901
    self: Any,
    record: _CatalogRecord,
    *,
    binding: Mapping[str, Any],
    run_dir: Path,
    selected_index: int,
    binding_index: int,
) -> dict[str, Any]:
    """Read and validate exactly one physical source observation with bounded retry.

    Retry semantics are published in the contract: max_retries=3, exponential
    backoff base 0.5s capped at 8s with 0-10% jitter, Retry-After handling for
    integer seconds and http-date, invalid/negative values ignored, jitter
    only on backoff. A source key has at most max_retries+1 transport requests
    cumulative across restarts; validation attempts are separate. Permanent
    4xx and deterministic failures never retry; retryable exhaustion becomes
    terminal failed.
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

    # Resolve retry config: catalog-level or default 3
    max_retries = 3
    retry_cfg = RetryConfig(max_retries=max_retries)
    # Allow transport to override via explicit max_retries for tests
    if isinstance(transport.get("max_retries"), int):
        max_retries = int(transport["max_retries"])
        retry_cfg = RetryConfig(
            max_retries=max_retries, base_delay=0.5, max_delay=8.0, jitter=True
        )
    # Support attempt_sequence for validator-driven matrix
    attempt_sequence = transport.get("attempt_sequence")
    if not isinstance(attempt_sequence, list):
        attempt_sequence = None

    attempt: dict[str, Any] = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "stable_key": record.stable_key,
        "selected_index": selected_index,
        "binding_index": binding_index,
        "attempt": binding_index + 1,
        "attempt_number": 1,
        "transport_attempt": 1,
        "cumulative_transport_attempts": 0,
        "max_retries": max_retries,
        "validation_attempt": 0,
        "adapter": transport["adapter"],
        "status": transport["status"],
        "source_kind": source_kind,
        "source_role": role,
        "role": role,
        "authority_role": role,
        "source_ref": _source_identity(source_ref),
        "retry_decision": False,
        "retry_history": [],
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
        "class": "unknown",
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

    # Bounded retry loop for transport phase only.
    # Deterministic parse/validation failures never re-enter this loop.
    # For simple fixture without attempt_sequence, single status governs
    # outcome directly: 429 stays retryable, 404 stays failed, 200 proceeds.
    # Only when attempt_sequence is provided do we iterate to simulate
    # transient-then-success or always-transient within same binding.
    transport_history: list[dict[str, Any]] = []
    final_status: int | None = None
    last_error: dict[str, Any] | None = None
    if attempt_sequence is None:
        cur_status = int(transport["status"])
        cur_retry_after = transport.get("retry_after", transport.get("Retry-After"))
        is_timeout = bool(transport.get("timeout")) or cur_status == 0
        if is_timeout and transport.get("timeout"):
            cur_status = 0
        attempt["status"] = cur_status
        attempt["attempt_number"] = 1
        attempt["transport_attempt"] = 1
        attempt["cumulative_transport_attempts"] = 1
        if cur_status == 0:
            cls = "timeout"
            retryable = True
            err_code = "transport_timeout"
            err_msg = "fixture transport timeout"
        elif cur_status >= 400:
            if is_retryable_status(cur_status):
                cls = "transient"
                retryable = True
                err_code = "transport_retryable"
                err_msg = f"fixture transport status {cur_status}"
            else:
                cls = "permanent"
                retryable = False
                err_code = "transport_permanent"
                err_msg = f"fixture transport status {cur_status}"
        else:
            cls = "success"
            retryable = False
            err_code = None  # type: ignore[assignment]
            err_msg = None  # type: ignore[assignment]
        attempt["class"] = cls
        attempt["retry_decision"] = retryable and max_retries > 0
        if cls in {"transient", "timeout"}:
            backoff = calculate_backoff(
                0,
                base_delay=retry_cfg.base_delay,
                max_delay=retry_cfg.max_delay,
                jitter=retry_cfg.jitter,
            )
            retry_after_delay = _parse_retry_after(cur_retry_after)
            delay = (
                max(backoff, retry_after_delay)
                if retry_after_delay is not None
                else backoff
            )
            transport_history.append(
                {
                    "attempt_number": 1,
                    "class": cls,
                    "status": cur_status,
                    "retry_decision": retryable,
                    "delay_seconds": delay if retryable else 0,
                    "scheduled_at": datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "retry_after_raw": cur_retry_after,
                    "retry_after_parsed": retry_after_delay,
                    "max_retries": max_retries,
                    "cumulative": 1,
                }
            )
            attempt["retry_history"] = list(transport_history)
            if retryable:
                return finish(
                    outcome="retryable",
                    transport_result="retryable",
                    error={
                        "code": err_code,
                        "message": err_msg,
                        "retry_after": cur_retry_after,
                        "delay_seconds": delay,
                    },
                )
            else:
                return finish(
                    outcome="failed",
                    transport_result="permanent_failure",
                    error={"code": err_code, "message": err_msg},
                )
        elif cls == "permanent":
            transport_history.append(
                {
                    "attempt_number": 1,
                    "class": cls,
                    "status": cur_status,
                    "retry_decision": False,
                    "delay_seconds": 0,
                    "scheduled_at": datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "retry_after_raw": cur_retry_after,
                    "max_retries": max_retries,
                    "cumulative": 1,
                }
            )
            attempt["retry_history"] = list(transport_history)
            return finish(
                outcome="failed",
                transport_result="permanent_failure",
                error={"code": err_code, "message": err_msg},
            )
        else:
            # success: proceed to file handling below
            attempt["retry_history"] = [
                {
                    "attempt_number": 1,
                    "class": "success",
                    "status": cur_status,
                    "retry_decision": False,
                    "delay_seconds": 0,
                    "scheduled_at": datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "max_retries": max_retries,
                    "cumulative": 1,
                }
            ]
            final_status = cur_status
    else:
        for transport_attempt in range(min(max_retries + 1, len(attempt_sequence))):
            seq_entry = attempt_sequence[transport_attempt]
            if isinstance(seq_entry, dict):
                cur_status = int(seq_entry.get("status", transport["status"]))
                cur_retry_after = seq_entry.get(
                    "retry_after", seq_entry.get("Retry-After")
                )
            else:
                cur_status = int(seq_entry)
                cur_retry_after = None
            # Timeout simulation
            if transport.get("timeout") and transport_attempt == 0:
                cur_status = 0
                is_timeout = True
            else:
                is_timeout = cur_status == 0
            attempt["status"] = cur_status
            attempt["attempt_number"] = transport_attempt + 1
            attempt["transport_attempt"] = transport_attempt + 1
            attempt["cumulative_transport_attempts"] = transport_attempt + 1
            if is_timeout:
                cls = "timeout"
                retryable = True
                err_code = "transport_timeout"
                err_msg = "fixture transport timeout"
            elif cur_status >= 400:
                if is_retryable_status(cur_status):
                    cls = "transient"
                    retryable = True
                    err_code = "transport_retryable"
                    err_msg = f"fixture transport status {cur_status}"
                else:
                    cls = "permanent"
                    retryable = False
                    err_code = "transport_permanent"
                    err_msg = f"fixture transport status {cur_status}"
            else:
                cls = "success"
                retryable = False
                err_code = None  # type: ignore[assignment]
                err_msg = None  # type: ignore[assignment]
            attempt["class"] = cls
            attempt["retry_decision"] = retryable and transport_attempt < max_retries
            if (
                cls in {"transient", "timeout"}
                and transport_attempt < max_retries
                and transport_attempt < len(attempt_sequence) - 1
            ):
                backoff = calculate_backoff(
                    transport_attempt,
                    base_delay=retry_cfg.base_delay,
                    max_delay=retry_cfg.max_delay,
                    jitter=retry_cfg.jitter,
                )
                retry_after_delay = _parse_retry_after(cur_retry_after)
                delay = (
                    max(backoff, retry_after_delay)
                    if retry_after_delay is not None
                    else backoff
                )
                transport_history.append(
                    {
                        "attempt_number": transport_attempt + 1,
                        "class": cls,
                        "status": cur_status,
                        "retry_decision": True,
                        "delay_seconds": delay,
                        "scheduled_at": datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "retry_after_raw": cur_retry_after,
                        "retry_after_parsed": retry_after_delay,
                        "max_retries": max_retries,
                        "cumulative": transport_attempt + 1,
                    }
                )
                attempt["retry_history"] = list(transport_history)
                continue
            if cls in {"transient", "timeout"}:
                if (
                    transport_attempt >= max_retries
                    or transport_attempt == len(attempt_sequence) - 1
                ):
                    # Last attempt in sequence: record and decide exhaustion vs retryable
                    is_last = transport_attempt == len(attempt_sequence) - 1
                    # If sequence ends with transient and not exhausted, treat as retryable (preserve cursor)
                    # If sequence length == max_retries+1 and last is transient, it's exhausted -> failed
                    if (
                        is_last
                        and cls in {"transient", "timeout"}
                        and len(attempt_sequence) <= max_retries
                    ):
                        # Not exhausted, still retryable
                        transport_history.append(
                            {
                                "attempt_number": transport_attempt + 1,
                                "class": cls,
                                "status": cur_status,
                                "retry_decision": True,
                                "delay_seconds": 0,
                                "scheduled_at": datetime.now(UTC)
                                .isoformat()
                                .replace("+00:00", "Z"),
                                "retry_after_raw": cur_retry_after,
                                "max_retries": max_retries,
                                "cumulative": transport_attempt + 1,
                            }
                        )
                        attempt["retry_history"] = list(transport_history)
                        final_status = cur_status
                        last_error = {
                            "code": err_code,
                            "message": err_msg,
                            "retryable": True,
                        }
                        break
                    transport_history.append(
                        {
                            "attempt_number": transport_attempt + 1,
                            "class": cls,
                            "status": cur_status,
                            "retry_decision": False,
                            "delay_seconds": 0,
                            "scheduled_at": datetime.now(UTC)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "retry_after_raw": cur_retry_after,
                            "retry_after_parsed": _parse_retry_after(cur_retry_after),
                            "max_retries": max_retries,
                            "cumulative": transport_attempt + 1,
                            "exhausted": transport_attempt >= max_retries,
                        }
                    )
                    attempt["retry_history"] = list(transport_history)
                    final_status = cur_status
                    last_error = {
                        "code": err_code,
                        "message": err_msg,
                        "exhausted": transport_attempt >= max_retries,
                    }
                    break
                continue
            if cls == "permanent":
                transport_history.append(
                    {
                        "attempt_number": transport_attempt + 1,
                        "class": cls,
                        "status": cur_status,
                        "retry_decision": False,
                        "delay_seconds": 0,
                        "scheduled_at": datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "retry_after_raw": cur_retry_after,
                        "max_retries": max_retries,
                        "cumulative": transport_attempt + 1,
                    }
                )
                attempt["retry_history"] = list(transport_history)
                final_status = cur_status
                last_error = {"code": err_code, "message": err_msg}
                break
            if cls == "success":
                if transport_history:
                    transport_history.append(
                        {
                            "attempt_number": transport_attempt + 1,
                            "class": "success",
                            "status": cur_status,
                            "retry_decision": False,
                            "delay_seconds": 0,
                            "scheduled_at": datetime.now(UTC)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "max_retries": max_retries,
                            "cumulative": transport_attempt + 1,
                        }
                    )
                    attempt["retry_history"] = list(transport_history)
                else:
                    attempt["retry_history"] = [
                        {
                            "attempt_number": 1,
                            "class": "success",
                            "status": cur_status,
                            "retry_decision": False,
                            "delay_seconds": 0,
                            "scheduled_at": datetime.now(UTC)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "max_retries": max_retries,
                            "cumulative": 1,
                        }
                    ]
                final_status = cur_status
                break

    # After loop, handle retryable vs permanent vs success
    if final_status is None:
        # Should not happen
        final_status = int(transport["status"])
    if (
        attempt.get("class") in {"transient", "timeout"}
        and last_error
        and last_error.get("exhausted")
    ):
        # Exhausted retryable becomes failed, not retryable
        attempt["retry_decision"] = False
        return finish(
            outcome="failed",
            transport_result="exhausted",
            error={
                "code": "transport_exhausted",
                "message": f"exhausted {max_retries + 1} attempts",
                "last_status": final_status,
            },
        )
    status = final_status
    if status >= 400:
        # Already handled permanent vs exhausted
        if is_retryable_status(status) or status == 0:
            # This is retryable but we exhausted? Already handled above
            # If we are here with retryable and not exhausted, it means we have retries remaining but chose not to retry?
            # Return retryable
            retryable = status == 0 or is_retryable_status(status)
            attempt["retry_decision"] = retryable
            return finish(
                outcome="retryable" if retryable else "failed",
                transport_result="retryable" if retryable else "permanent_failure",
                error={
                    "code": "transport_retryable"
                    if retryable
                    else "transport_permanent",
                    "message": f"fixture transport status {status}",
                },
            )
        else:
            return finish(
                outcome="failed",
                transport_result="permanent_failure",
                error={
                    "code": "transport_permanent",
                    "message": f"fixture transport status {status}",
                },
            )
    # Success status path continues to file handling below: use final_status
    status = final_status  # type: ignore[assignment]
    # If we simulated success after transient, ensure status is success
    if (
        attempt.get("retry_history")
        and any(h.get("class") == "transient" for h in attempt["retry_history"])
        and status >= 400
    ):
        # We retried a transient and now succeed via file
        status = 200
        attempt["status"] = 200
    if status >= 400:
        # Should have been handled, but fallback
        retryable = is_retryable_status(status)
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
