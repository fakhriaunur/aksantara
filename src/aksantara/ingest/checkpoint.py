"""Deterministic, caller-owned corpus checkpoint driver.

The checkpoint driver is deliberately local and transport-only.  It accepts a
JSON-compatible catalog whose records bind a normalized stable key to an
approved fixture adapter, a verified :class:`~aksantara.domain.models.SourceRef`,
and an expected raw hash.  It never creates a release, changes a pointer, or
writes the canonical namespace.

The public contract is ``scripts/checkpoint.py --help`` and the
``/checkpoints/*`` API routes.  The implementation below is also useful to
callers that need a deterministic core without starting the API.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksantara.ingest.checkpoint_authority import CheckpointAuthorityMixin
from aksantara.ingest.checkpoint_candidate import CheckpointCandidateMixin
from aksantara.ingest.checkpoint_catalog import (
    _catalog_records,
    _validate_limit,
    normalize_stable_key,
    selection_keys,
)
from aksantara.ingest.checkpoint_execution import CheckpointExecutionMixin
from aksantara.ingest.checkpoint_storage import (
    _hash_payload,
    _read_json,
    _write_state_json,
)
from aksantara.ingest.checkpoint_types import (
    _CONTROL_RE,
    _FALLBACK_HOSTS,
    _OFFICIAL_HOSTS,
    _OUTCOMES,
    _RUN_ID_RE,
    AUTHORITY_POLICY_VERSION,
    CATALOG_SCHEMA_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    COMPARISON_POLICY_VERSION,
    DEFAULT_LIMIT,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_KEY_LENGTH,
    MAX_LIMIT,
    SELECTION_ALGORITHM,
    TRANSFORM_VERSION,
    CatalogValidationError,
    CheckpointConflictError,
    CheckpointError,
    CheckpointNotFoundError,
    CheckpointPersistenceError,
    CheckpointPreflight,
    LimitValidationError,
    RunResult,
)
from aksantara.parse.parser_contract import PARSER_VERSION
from aksantara.validate.conflicts import LEXICAL_FIELDS
from aksantara.validate.review import ReviewStore

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CHECKPOINT_SCHEMA_VERSION",
    "CatalogValidationError",
    "CheckpointConflictError",
    "CheckpointDriver",
    "CheckpointError",
    "CheckpointNotFoundError",
    "CheckpointPersistenceError",
    "CheckpointPreflight",
    "LimitValidationError",
    "RunResult",
    "normalize_stable_key",
    "selection_keys",
]


class CheckpointDriver(
    CheckpointExecutionMixin,
    CheckpointAuthorityMixin,
    CheckpointCandidateMixin,
):
    """Run a bounded deterministic checkpoint using caller-owned fixtures."""

    _lock = threading.RLock()

    def __init__(self, *, root: Path | str) -> None:
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = Path.cwd() / root_path
        self.root = root_path.resolve()
        self.state_root = self.root / ".aksantara" / "checkpoint-runs"
        self.idempotency_path = self.root / ".aksantara" / "checkpoint-idempotency.json"

    @staticmethod
    def contract() -> dict[str, Any]:
        """Return the machine-readable public checkpoint contract."""
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "catalog_schema": CATALOG_SCHEMA_VERSION,
            "mode": "local-fixture-only",
            "description": (
                "Select a deterministic bounded prefix from a caller-owned "
                "catalog and evaluate only those source bindings."
            ),
            "key_normalization": {
                "algorithm": "Unicode NFKC, collapse Unicode whitespace, casefold",
                "rejects": [
                    "blank keys",
                    "control characters",
                    "slash or backslash",
                    "dot, dot-dot, and keys containing '..'",
                    "URL-like keys",
                    f"keys longer than {MAX_KEY_LENGTH} characters",
                ],
            },
            "limit": {
                "default": DEFAULT_LIMIT,
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "domain": "integer 1..100 inclusive",
                "above_maximum": "reject before run creation",
                "short_catalog": "process available keys and mark exact shortfall/ineligible",
            },
            "selection": {
                "algorithm": SELECTION_ALGORITHM,
                "input": "sorted normalized stable_key",
                "rule": "take the first effective_limit keys; never pad or sample",
                "unique": True,
            },
            "fingerprints": {
                "catalog": "sha256(canonical sorted stable_key + source-reference identity records)",
                "run": (
                    "sha256(catalog fingerprint + corpus/version + effective limit + "
                    "selection + authority/comparison policy + parser/transform pins)"
                ),
                "idempotency_scope": (
                    "caller root is a separate scope boundary, not a release hash input"
                ),
                "source_reference_identity": [
                    "url",
                    "source_kind",
                    "edition",
                    "source_version",
                    "content_hash",
                    "parser_version",
                ],
                "volatile_fields_excluded": ["retrieved_at", "input order"],
            },
            "fixture_manifest": {
                "required_catalog_fields": ["catalog_id", "corpus_version", "entries"],
                "required_entry_fields": ["stable_key", "source_ref", "transport"],
                "optional_observation_fields": [
                    "role",
                    "source_ref",
                    "transport",
                ],
                "observation_roles": {
                    "official": "adapter-verified KBBI observation",
                    "fallback": "labelled evidence only; never canonical",
                    "evidence": "labelled non-authoritative evidence only",
                },
                "transport_adapter": "fixture",
                "binding": "relative path under root or immutable inline bytes",
                "changed_source_override": (
                    "inline content may replace a path binding; all other "
                    "multiple representations are rejected"
                ),
                "required_hash": "expected_raw_hash equals source_ref.content_hash and actual bytes",
                "comparison_modes": ["exact", "sha256"],
                "approved_hosts": sorted(_OFFICIAL_HOSTS | _FALLBACK_HOSTS),
                "live_network": False,
            },
            "roots": {
                "caller_owned": True,
                "state_layout": ".aksantara/checkpoint-runs/<run_id>/",
                "path_policy": "all state, raw, parsed, and report artifacts remain under root",
            },
            "pins": {
                "parser_version": PARSER_VERSION,
                "transform_version": TRANSFORM_VERSION,
                "validation_policy": AUTHORITY_POLICY_VERSION,
                "selection_algorithm": SELECTION_ALGORITHM,
                "comparison_policy": COMPARISON_POLICY_VERSION,
            },
            "idempotency": {
                "scope": "caller root + catalog fingerprint + run tuple",
                "preimage": "run fingerprint and explicit idempotency key",
                "same_tuple": "return existing durable run without source reads or writes",
                "changed_tuple": "structured conflict before source processing",
            },
            "lifecycle": {
                "states": {
                    "created": {"terminal": False},
                    "running": {"terminal": False},
                    "blocked": {"terminal": True, "resumable": False},
                    "failed": {"terminal": True, "resumable": False},
                    "completed": {"terminal": True, "resumable": False},
                },
                "item_outcomes": list(_OUTCOMES),
                "completion": (
                    "completed only after one current outcome for every selected key; "
                    "accepted-only 100-key data is still not a release"
                ),
            },
            "outcomes": {
                "accepted": "parsed and schema/authority validation succeeded; no candidate/pointer write",
                "rejected": "deterministic parse/schema/hash/input failure",
                "quarantined": "authority or review-required failure",
                "retryable": "transport status 429/5xx; not eligible",
                "failed": "permanent transport or durable processing failure",
            },
            "error_mapping": {
                "invalid_catalog_or_limit": "422 / CLI exit 2, no run or source attempt",
                "idempotency_conflict": "409 / CLI exit 2",
                "unknown_run": "404 / CLI exit 2",
                "durable_write_failure": "503 / CLI exit 1",
            },
            "promotion": {
                "candidate_created": False,
                "candidate_operation": "candidate-evaluate",
                "pointer_changed": False,
                "release_promotion": "never implicit; no current pointer operation",
            },
            "authority_review": {
                "lexical_fields": list(LEXICAL_FIELDS),
                "metadata_only_fields": [
                    "url",
                    "retrieved_at",
                    "edition",
                    "source_version",
                    "raw_sha256",
                    "parser_version",
                    "transform_version",
                ],
                "decisions": ["select_official", "block", "reject"],
                "history": "append-only and idempotent by decision key",
                "queue_order": "stable_key then review_id",
            },
            "candidate_gate": {
                "operation": "candidate-evaluate",
                "requires": [
                    "terminal current outcomes",
                    "adapter-verified official source",
                    "raw/canonical exact joins",
                    "resolved item review",
                    "fixed 100-key complete checkpoint",
                    "explicit release-level approval",
                    "release approver identity and reason",
                ],
                "vectors_created": False,
                "current_version_changed": False,
            },
            "network_trace": {
                "local_mode": True,
                "live_network_attempts": 0,
                "gcp_attempts": 0,
                "emulator_attempts": 0,
                "unapproved_host_attempts": 0,
            },
            "public_operations": [
                "contract/help",
                "create/start",
                "status",
                "report",
                "run history",
                "current outcomes",
                "attempt history",
                "idempotent execute/no-op",
                "review queue/read/decision",
                "candidate evaluation/read",
                "public read-only replay",
            ],
        }

    def preflight(
        self, catalog: Mapping[str, Any], *, limit: int | str | None = None
    ) -> CheckpointPreflight:
        """Validate a catalog without reading or writing source artifacts."""
        if not isinstance(catalog, Mapping):
            raise CatalogValidationError("catalog must be a JSON object")
        effective_limit = _validate_limit(limit)
        root_scope = str(self.root)
        catalog_id, corpus_version, records, metadata = _catalog_records(
            catalog,
            root=self.root,
        )
        sorted_records = tuple(sorted(records, key=lambda item: item.stable_key))
        catalog_preimage: dict[str, Any] = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_id": catalog_id,
            "corpus_version": corpus_version,
            "records": [record.fingerprint_record() for record in sorted_records],
        }
        catalog_fingerprint = _hash_payload(catalog_preimage)
        selected = sorted_records[:effective_limit]
        run_preimage: dict[str, Any] = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "catalog_id": catalog_id,
            "corpus_version": corpus_version,
            "catalog_fingerprint": catalog_fingerprint,
            "effective_limit": effective_limit,
            "selection_algorithm": SELECTION_ALGORITHM,
            "authority_mode": metadata["authority_mode"],
            "comparison_policy": metadata["comparison_policy"],
            "parser_version": metadata["parser_version"],
            "transform_version": metadata["transform_version"],
            "validation_policy": metadata["validation_policy"],
            "comparison_records": [
                {
                    "stable_key": record.stable_key,
                    "comparison_mode": record.transport["comparison_mode"],
                }
                for record in sorted_records
            ],
        }
        run_fingerprint = _hash_payload(run_preimage)
        return CheckpointPreflight(
            catalog_id=catalog_id,
            corpus_version=corpus_version,
            records=sorted_records,
            selected=selected,
            requested_limit=effective_limit,
            effective_limit=effective_limit,
            selection_algorithm=SELECTION_ALGORITHM,
            authority_policy=metadata["authority_mode"],
            comparison_policy=metadata["comparison_policy"],
            parser_version=metadata["parser_version"],
            transform_version=metadata["transform_version"],
            validation_policy=metadata["validation_policy"],
            catalog_fingerprint=catalog_fingerprint,
            run_fingerprint=run_fingerprint,
            catalog_preimage=catalog_preimage,
            run_preimage=run_preimage,
            root_scope=root_scope,
        )

    def run(
        self,
        catalog: Mapping[str, Any],
        *,
        limit: int | str | None = None,
        idempotency_key: str | None = None,
    ) -> RunResult:
        """Create and synchronously execute one durable local checkpoint."""
        with self._lock:
            preflight = self.preflight(catalog, limit=limit)
            effective_key = self._validate_idempotency_key(
                idempotency_key
                if idempotency_key is not None
                else f"run:{preflight.run_fingerprint}"
            )
            existing = self._find_idempotent(effective_key)
            if existing is not None:
                existing_fingerprint = existing.get("run_fingerprint")
                if existing_fingerprint != preflight.run_fingerprint:
                    raise CheckpointConflictError(
                        "idempotency key is bound to a different checkpoint tuple",
                        details={
                            "idempotency_key": effective_key,
                            "existing_run_id": existing.get("run_id"),
                            "existing_run_fingerprint": existing_fingerprint,
                            "requested_run_fingerprint": preflight.run_fingerprint,
                        },
                    )
                return self._load_result(str(existing["run_id"]))

            run_id = f"checkpoint-{preflight.run_fingerprint[:24]}"
            run_dir = self._run_dir(run_id)
            if (run_dir / "report.json").exists():
                stored = _read_json(run_dir / "preflight.json")
                if (
                    stored.get("fingerprints", {}).get("run")
                    != preflight.run_fingerprint
                ):
                    raise CheckpointConflictError(
                        "durable run identity conflicts with requested tuple",
                        details={"run_id": run_id},
                    )
                self._bind_idempotency(
                    effective_key,
                    run_id,
                    preflight.run_fingerprint,
                )
                return self._load_result(run_id)

            self._create_run(preflight, run_id, effective_key, catalog)
            result = self._execute(preflight, run_id, effective_key)
            self._bind_idempotency(
                effective_key,
                run_id,
                preflight.run_fingerprint,
            )
            return result

    def status(self, run_id: str) -> dict[str, Any]:
        run_dir = self._existing_run_dir(run_id)
        return _read_json(run_dir / "status.json")

    def report(self, run_id: str) -> dict[str, Any]:
        run_dir = self._existing_run_dir(run_id)
        return _read_json(run_dir / "report.json")

    def current_outcomes(self, run_id: str) -> dict[str, Any]:
        run_dir = self._existing_run_dir(run_id)
        payload = _read_json(run_dir / "outcomes.json")
        return {
            "run_id": run_id,
            "revision": payload.get("revision"),
            "snapshot": payload.get("snapshot"),
            "selected_count": payload.get("selected_count"),
            "outcomes": payload.get("outcomes", []),
        }

    def attempts(self, run_id: str) -> dict[str, Any]:
        run_dir = self._existing_run_dir(run_id)
        payload = _read_json(run_dir / "attempts.json")
        return {
            "run_id": run_id,
            "revision": payload.get("revision"),
            "attempt_count": payload.get("attempt_count"),
            "logical_attempt_count": payload.get(
                "logical_attempt_count", payload.get("attempt_count")
            ),
            "transport_attempt_count": payload.get(
                "transport_attempt_count", payload.get("attempt_count")
            ),
            "attempts": payload.get("attempts", []),
        }

    def history(self) -> dict[str, Any]:
        """Read immutable report revisions for every run under this root."""
        runs: list[dict[str, Any]] = []
        if not self.state_root.is_dir():
            return {
                "schema_version": "checkpoint-history-v1",
                "root_scope": str(self.root),
                "count": 0,
                "runs": runs,
            }
        for run_dir in sorted(self.state_root.iterdir(), key=lambda path: path.name):
            if not run_dir.is_dir() or not (run_dir / "report.json").is_file():
                continue
            report = _read_json(run_dir / "report.json")
            status = (
                _read_json(run_dir / "status.json")
                if (run_dir / "status.json").is_file()
                else {}
            )
            runs.append(
                {
                    "run_id": str(report.get("run_id", run_dir.name)),
                    "status": str(
                        status.get("status", report.get("status", "unknown"))
                    ),
                    "revision": int(report.get("revision", status.get("revision", 0))),
                    "snapshot": report.get("snapshot"),
                    "run_fingerprint": report.get("fingerprints", {}).get("run"),
                    "catalog_fingerprint": report.get("fingerprints", {}).get(
                        "catalog"
                    ),
                    "selected_count": report.get("selected_count", 0),
                    "processed_count": report.get("processed_count", 0),
                    "references": report.get(
                        "references", self._references(run_dir, run_dir.name)
                    ),
                    "immutable": True,
                }
            )
        return {
            "schema_version": "checkpoint-history-v1",
            "root_scope": str(self.root),
            "count": len(runs),
            "runs": runs,
        }

    def checkpoint(self, run_id: str) -> dict[str, Any]:
        run_dir = self._existing_run_dir(run_id)
        payload = _read_json(run_dir / "checkpoint.json")
        return payload

    def execute(self, run_id: str) -> RunResult:
        """Read the existing run as an idempotent execute/no-op operation."""
        return self._load_result(run_id)

    # Familiar lifecycle aliases keep the local core usable by callers that
    # describe the operation as start/create rather than run.
    start = run
    create = run

    def review_queue(self) -> list[dict[str, Any]]:
        """Read the deterministic open authority review queue."""
        return ReviewStore(root=self.root).list_open()

    def review_read(self, review_id: str) -> dict[str, Any]:
        """Read one review record, including immutable source evidence."""
        return ReviewStore(root=self.root).get(review_id)

    def review_decide(
        self,
        review_id: str,
        *,
        decision: str,
        reviewer: str,
        reason: str,
        policy_version: str,
        idempotency_key: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Record an explicit append-only review decision."""
        return ReviewStore(root=self.root).decide(
            review_id,
            decision=decision,
            reviewer=reviewer,
            reason=reason,
            policy_version=policy_version,
            idempotency_key=idempotency_key,
            timestamp=timestamp,
        )

    def _validate_idempotency_key(self, value: str) -> str:
        if not isinstance(value, str):
            raise CheckpointConflictError("idempotency_key must be a string")
        value = value.strip()
        if not value or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise CheckpointConflictError(
                "idempotency_key must be 1..256 characters",
                details={"max_length": MAX_IDEMPOTENCY_KEY_LENGTH},
            )
        if _CONTROL_RE.search(value):
            raise CheckpointConflictError(
                "idempotency_key contains a control character"
            )
        return value

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise CheckpointNotFoundError(
                "run_id has an invalid format",
                details={"run_id": run_id},
            )
        return self.state_root / run_id

    def _existing_run_dir(self, run_id: str) -> Path:
        run_dir = self._run_dir(run_id)
        if not (run_dir / "status.json").is_file():
            raise CheckpointNotFoundError(
                "checkpoint run was not found",
                details={"run_id": run_id},
            )
        return run_dir

    def _load_result(self, run_id: str) -> RunResult:
        run_dir = self._existing_run_dir(run_id)
        status = _read_json(run_dir / "status.json")
        report = _read_json(run_dir / "report.json")
        return RunResult(
            run_id=run_id,
            status=str(status.get("status", report.get("status", "unknown"))),
            revision=int(status.get("revision", report.get("revision", 0))),
            report=report,
        )

    def _find_idempotent(self, key: str) -> dict[str, Any] | None:
        if not self.idempotency_path.is_file():
            return None
        payload = _read_json(self.idempotency_path)
        bindings = payload.get("bindings", {})
        if not isinstance(bindings, Mapping):
            raise CheckpointPersistenceError(
                "idempotency index is malformed",
                details={"path": str(self.idempotency_path)},
            )
        value = bindings.get(key)
        return dict(value) if isinstance(value, Mapping) else None

    def _bind_idempotency(self, key: str, run_id: str, run_fingerprint: str) -> None:
        bindings: dict[str, Any] = {}
        if self.idempotency_path.is_file():
            existing = _read_json(self.idempotency_path)
            raw_bindings = existing.get("bindings", {})
            if isinstance(raw_bindings, Mapping):
                bindings = {str(k): v for k, v in raw_bindings.items()}
        current = bindings.get(key)
        value = {
            "run_id": run_id,
            "run_fingerprint": run_fingerprint,
        }
        if current is not None and current != value:
            raise CheckpointConflictError(
                "idempotency index contains a conflicting binding",
                details={"idempotency_key": key},
            )
        bindings[key] = value
        _write_state_json(
            self.idempotency_path,
            {
                "schema_version": "checkpoint-idempotency-v1",
                "root_scope": str(self.root),
                "bindings": bindings,
            },
            self.root,
        )
