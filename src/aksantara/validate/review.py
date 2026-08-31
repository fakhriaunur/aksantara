"""Durable, deterministic authority-conflict review state.

The review store is intentionally small and caller-rooted.  Conflict source
side payloads are immutable evidence; only the review status and an
append-only decision history can change.  A filesystem-backed store is used
by the checkpoint driver and API, while an in-memory mode keeps the pure
domain seam convenient for unit tests.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aksantara.domain.provenance import canonical_json_hash
from aksantara.ingest.checkpoint_storage import (
    _canonical_bytes,
    _read_json,
    _write_immutable,
    _write_state_json,
)

REVIEW_SCHEMA_VERSION = "authority-review-v1"
DECISIONS = frozenset({"select_official", "block", "reject"})
OPEN_REVIEW_STATUSES = frozenset({"pending", "quarantined"})


class ReviewError(ValueError):
    """Base for structured review-store failures."""


class ReviewNotFoundError(ReviewError):
    """A conflict or quarantine record does not exist."""


class ReviewDecisionConflictError(ReviewError):
    """An idempotency key was reused for another decision payload."""


def _iso(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _review_id(prefix: str, payload: dict[str, Any]) -> str:
    return f"{prefix}-{canonical_json_hash(payload)[:32]}"


def _review_dir(root: Path, kind: str) -> Path:
    return root / ".aksantara" / "review" / kind


class ReviewStore:
    """Persist conflicts, quarantines, and explicit review decisions.

    Args:
        root: Caller-owned artifact root.  When omitted, records live only in
            this instance and are still immutable with respect to source-side
            payloads.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else None
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Durable record helpers
    # ------------------------------------------------------------------

    def _path(self, review_id: str, kind: str | None = None) -> Path:
        if self.root is None:
            raise ReviewError("review store has no caller-owned root")
        effective_kind = kind or (
            "conflicts" if review_id.startswith("conflict-") else "quarantine"
        )
        return _review_dir(self.root, effective_kind) / f"{review_id}.json"

    def _load(self, review_id: str) -> dict[str, Any]:
        with self._lock:
            if self.root is None:
                value = self._records.get(review_id)
                if value is None:
                    raise ReviewNotFoundError(f"review record not found: {review_id}")
                return cast(dict[str, Any], _clone(value))
            for kind in ("conflicts", "quarantine"):
                path = self._path(review_id, kind)
                if path.is_file():
                    return _read_json(path)
        raise ReviewNotFoundError(f"review record not found: {review_id}")

    def _persist_new(self, record: dict[str, Any], *, kind: str) -> dict[str, Any]:
        review_id = str(record["review_id"])
        with self._lock:
            if self.root is None:
                existing = self._records.get(review_id)
                if existing is not None:
                    _assert_source_evidence_unchanged(existing, record)
                    return cast(dict[str, Any], _clone(existing))
                self._records[review_id] = _clone(record)
                return cast(dict[str, Any], _clone(record))
            path = self._path(review_id, kind)
            if path.is_file():
                existing = _read_json(path)
                _assert_source_evidence_unchanged(existing, record)
                return existing
            _write_immutable(path, _canonical_bytes(record), self.root)
            return record

    def _persist_current(self, record: dict[str, Any], *, kind: str) -> dict[str, Any]:
        review_id = str(record["review_id"])
        with self._lock:
            if self.root is None:
                self._records[review_id] = _clone(record)
                return cast(dict[str, Any], _clone(record))
            _write_state_json(self._path(review_id, kind), record, self.root)
            return record

    # ------------------------------------------------------------------
    # Creation and deterministic queue reads
    # ------------------------------------------------------------------

    def persist_conflict(
        self,
        *,
        entry_id: str,
        stable_key: str,
        official: dict[str, Any],
        fallback: dict[str, Any],
        differing_fields: list[str],
        field_diffs: list[dict[str, Any]],
        first_seen_run: str,
        policy_version: str,
        created_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Create or return an immutable conflict record for two source sides."""
        identity = {
            "entry_id": entry_id,
            "stable_key": stable_key,
            "official_raw_sha256": official.get("raw_sha256", ""),
            "fallback_raw_sha256": fallback.get("raw_sha256", ""),
            "differing_fields": list(differing_fields),
            "policy_version": policy_version,
        }
        record: dict[str, Any] = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "review_id": _review_id("conflict", identity),
            "conflict_id": _review_id("conflict", identity),
            "type": "lexical_conflict",
            "entry_id": entry_id,
            "stable_key": stable_key,
            "first_seen_run": first_seen_run,
            "created_at": _iso(created_at),
            "review_status": "pending",
            "release_blocking": True,
            "decision_required": True,
            "selected_authority": None,
            "differing_fields": list(differing_fields),
            "field_diffs": [dict(item) for item in field_diffs],
            "official": _clone(official),
            "fallback": _clone(fallback),
            "review_history": [],
            "idempotency": {},
            "source_evidence_immutable": True,
            "policy_version": policy_version,
        }
        return self._persist_new(record, kind="conflicts")

    def persist_quarantine(
        self,
        *,
        entry_id: str,
        stable_key: str,
        reason: str,
        source: dict[str, Any],
        first_seen_run: str,
        policy_version: str,
        details: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Persist a non-conflict quarantine as a reviewable blocked record."""
        identity = {
            "entry_id": entry_id,
            "stable_key": stable_key,
            "reason": reason,
            "raw_sha256": source.get("raw_sha256", ""),
            "policy_version": policy_version,
        }
        record: dict[str, Any] = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "review_id": _review_id("quarantine", identity),
            "quarantine_id": _review_id("quarantine", identity),
            "type": "quarantine",
            "entry_id": entry_id,
            "stable_key": stable_key,
            "reason": reason,
            "details": details,
            "first_seen_run": first_seen_run,
            "created_at": _iso(created_at),
            "review_status": "quarantined",
            "release_blocking": True,
            "decision_required": True,
            "selected_authority": None,
            "source": _clone(source),
            "review_history": [],
            "idempotency": {},
            "source_evidence_immutable": True,
            "policy_version": policy_version,
        }
        return self._persist_new(record, kind="quarantine")

    def get(self, review_id: str) -> dict[str, Any]:
        """Read a review record, including both conflict source sides."""
        return self._load(review_id)

    read = get

    def _all_records(self) -> list[dict[str, Any]]:
        with self._lock:
            if self.root is None:
                values = [_clone(value) for value in self._records.values()]
            else:
                values = []
                for kind in ("conflicts", "quarantine"):
                    directory = _review_dir(self.root, kind)
                    if not directory.is_dir():
                        continue
                    for path in sorted(directory.glob("*.json")):
                        try:
                            values.append(_read_json(path))
                        except ReviewError:
                            raise
            values.sort(
                key=lambda value: (
                    str(value.get("stable_key", value.get("entry_id", ""))),
                    str(value.get("review_id", "")),
                )
            )
            return values

    def list_all(self) -> list[dict[str, Any]]:
        """Return all review records in deterministic queue order."""
        return self._all_records()

    def list_open(self) -> list[dict[str, Any]]:
        """Return unresolved/release-blocking records in stable order."""
        return [
            value
            for value in self._all_records()
            if value.get("review_status") in OPEN_REVIEW_STATUSES
        ]

    list_queue = list_open

    # ------------------------------------------------------------------
    # Append-only decisions
    # ------------------------------------------------------------------

    def decide(
        self,
        review_id: str,
        *,
        decision: str,
        reviewer: str,
        reason: str,
        policy_version: str,
        idempotency_key: str | None = None,
        timestamp: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Append one explicit decision and return the new current record.

        ``select_official`` is the only selecting decision.  A fallback or
        non-authoritative source can never be selected.  Repeating the same
        idempotency key with the same payload is a no-op; a changed payload is
        a typed conflict.
        """
        if decision not in DECISIONS:
            raise ReviewError(f"decision must be one of {sorted(DECISIONS)}")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ReviewError("reviewer must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ReviewError("reason must be non-empty")
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise ReviewError("policy_version must be non-empty")

        current = self._load(review_id)
        if decision == "select_official" and "official" not in current:
            raise ReviewError(
                "select_official is only valid for a conflict with an official side"
            )
        key = idempotency_key or _review_id(
            "decision",
            {
                "review_id": review_id,
                "decision": decision,
                "reviewer": reviewer.strip(),
                "reason": reason.strip(),
                "policy_version": policy_version,
            },
        )
        payload = {
            "decision": decision,
            "reviewer": reviewer.strip(),
            "reason": reason.strip(),
            "policy_version": policy_version,
        }
        prior = current.get("idempotency", {}).get(key)
        if prior is not None:
            if prior != payload:
                raise ReviewDecisionConflictError(
                    f"review decision idempotency key conflicts: {key}"
                )
            return current

        event = {
            "event_id": _review_id(
                "review-event",
                {"review_id": review_id, "key": key, **payload},
            ),
            "idempotency_key": key,
            **payload,
            "timestamp": _iso(timestamp),
        }
        history = list(current.get("review_history", []))
        history.append(event)
        updated = _clone(current)
        updated["review_history"] = history
        updated.setdefault("idempotency", {})[key] = payload
        updated["last_decision"] = decision
        updated["decision"] = decision
        updated["event_id"] = event["event_id"]
        updated["selected_authority"] = (
            "official" if decision == "select_official" else None
        )
        if decision == "select_official":
            updated["review_status"] = "approved"
            # The source conflict is resolved at item level.  A separate
            # release-level approval is still required by candidate gates, so
            # the record remains release-blocking until that gate is explicit.
            updated["release_blocking"] = True
        else:
            updated["review_status"] = "rejected"
            updated["release_blocking"] = True
        kind = "conflicts" if review_id.startswith("conflict-") else "quarantine"
        _assert_source_evidence_unchanged(current, updated)
        return self._persist_current(updated, kind=kind)


def _clone(value: Any) -> Any:
    """Clone JSON-compatible state without sharing mutable source payloads."""
    if isinstance(value, dict):
        return {str(key): _clone(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_clone(child) for child in value]
    return value


def _assert_source_evidence_unchanged(
    before: dict[str, Any], after: dict[str, Any]
) -> None:
    """Reject any attempt to mutate the stored source-side evidence."""
    immutable_keys = (
        "review_id",
        "conflict_id",
        "quarantine_id",
        "type",
        "entry_id",
        "stable_key",
        "first_seen_run",
        "official",
        "fallback",
        "source",
        "differing_fields",
        "field_diffs",
        "reason",
        "details",
        "source_evidence_immutable",
    )
    for key in immutable_keys:
        if key in before and before.get(key) != after.get(key):
            raise ReviewError(f"immutable review evidence changed: {key}")


__all__ = [
    "DECISIONS",
    "OPEN_REVIEW_STATUSES",
    "REVIEW_SCHEMA_VERSION",
    "ReviewDecisionConflictError",
    "ReviewError",
    "ReviewNotFoundError",
    "ReviewStore",
]
