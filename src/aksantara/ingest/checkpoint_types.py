"""Value objects and errors shared by checkpoint adapters."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from aksantara.domain.errors import AksantaraDomainError
from aksantara.domain.models import SourceRef

CATALOG_SCHEMA_VERSION = "checkpoint-catalog-v1"
CHECKPOINT_SCHEMA_VERSION = "checkpoint-run-v1"
SELECTION_ALGORITHM = "sorted-normalized-stable-key-v1"
AUTHORITY_POLICY_VERSION = "official-first-v1"
COMPARISON_POLICY_VERSION = "sha256-exact-v1"
TRANSFORM_VERSION = "0.1.0"
DEFAULT_LIMIT = 100
MAX_LIMIT = 100
MAX_KEY_LENGTH = 256
MAX_IDEMPOTENCY_KEY_LENGTH = 256
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^checkpoint-[0-9a-f]{16,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ALLOWED_SOURCE_KINDS = {
    "official-live",
    "official-snapshot",
    "rule",
    "sipebi",
    "gov-derived",
    "fallback",
    "enrichment",
    "evaluation",
    "ai-proposal",
}
_OFFICIAL_SOURCE_KINDS = {"official-live", "official-snapshot"}
_OFFICIAL_HOSTS = {"kbbi.kemdikbud.go.id"}
_FALLBACK_HOSTS = {"kbbi.web.id"}
_ALLOWED_FIXTURE_ADAPTERS = {"fixture", "local-fixture"}
_ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "text/html",
    "text/plain",
}
_OUTCOMES = (
    "pending",
    "in_progress",
    "accepted",
    "retryable",
    "quarantined",
    "rejected",
    "failed",
)
_TERMINAL_OUTCOMES = {"accepted", "quarantined", "rejected", "failed"}


class CheckpointError(AksantaraDomainError):
    """Base class for structured checkpoint failures."""

    code = "checkpoint_error"
    status_code = 400

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


class CatalogValidationError(CheckpointError):
    """Catalog or source identity failed preflight."""

    code = "invalid_catalog"
    status_code = 422


class LimitValidationError(CheckpointError):
    """Requested checkpoint limit is outside the published domain."""

    code = "invalid_limit"
    status_code = 422


class CheckpointConflictError(CheckpointError):
    """An idempotency key was reused with a different material tuple."""

    code = "idempotency_conflict"
    status_code = 409


class CheckpointNotFoundError(CheckpointError):
    """A requested durable run or artifact reference does not exist."""

    code = "run_not_found"
    status_code = 404


class CheckpointPersistenceError(CheckpointError):
    """A caller-owned durable write could not be completed safely."""

    code = "persistence_error"
    status_code = 503


@dataclass(frozen=True, slots=True)
class _CatalogRecord:
    stable_key: str
    source_ref: SourceRef
    transport: dict[str, Any]
    ordinal: int

    @property
    def source_identity(self) -> dict[str, str]:
        """Return the non-volatile source identity used in fingerprints."""
        return {
            "url": self.source_ref.url,
            "source_kind": self.source_ref.source_kind,
            "edition": self.source_ref.edition,
            "source_version": self.source_ref.source_version,
            "content_hash": self.source_ref.content_hash,
            "parser_version": self.source_ref.parser_version,
        }

    def fingerprint_record(self) -> dict[str, Any]:
        return {
            "stable_key": self.stable_key,
            "source_ref": self.source_identity,
        }

    def public_dict(self) -> dict[str, Any]:
        binding: dict[str, Any] = {
            "adapter": self.transport["adapter"],
            "comparison_mode": self.transport["comparison_mode"],
            "content_type": self.transport["content_type"],
            "expected_raw_hash": self.transport["expected_raw_hash"],
        }
        if "path" in self.transport:
            binding["path"] = self.transport["path"]
        else:
            binding["binding"] = "inline-immutable-bytes"
        return {
            "stable_key": self.stable_key,
            "source_ref": _source_ref_dict(self.source_ref),
            "transport": binding,
        }


@dataclass(frozen=True, slots=True)
class CheckpointPreflight:
    """Immutable result of catalog validation and stable selection."""

    catalog_id: str
    corpus_version: str
    records: tuple[_CatalogRecord, ...]
    selected: tuple[_CatalogRecord, ...]
    requested_limit: int
    effective_limit: int
    selection_algorithm: str
    authority_policy: str
    comparison_policy: str
    parser_version: str
    transform_version: str
    validation_policy: str
    catalog_fingerprint: str
    run_fingerprint: str
    catalog_preimage: dict[str, Any]
    run_preimage: dict[str, Any]
    root_scope: str

    @property
    def selected_keys(self) -> list[str]:
        return [record.stable_key for record in self.selected]

    @property
    def shortfall(self) -> int:
        return max(0, self.effective_limit - len(self.records))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog": {
                "id": self.catalog_id,
                "corpus_version": self.corpus_version,
                "record_count": len(self.records),
                "root_scope": self.root_scope,
            },
            "selection": {
                "algorithm": self.selection_algorithm,
                "requested_limit": self.requested_limit,
                "effective_limit": self.effective_limit,
                "selected_count": len(self.selected),
                "shortfall": self.shortfall,
                "selected_keys": self.selected_keys,
            },
            "authority_policy": self.authority_policy,
            "comparison_policy": self.comparison_policy,
            "pins": {
                "parser_version": self.parser_version,
                "transform_version": self.transform_version,
                "validation_policy": self.validation_policy,
            },
            "fingerprints": {
                "catalog": self.catalog_fingerprint,
                "run": self.run_fingerprint,
                "preimages": {
                    "catalog": self.catalog_preimage,
                    "run": self.run_preimage,
                },
            },
            "records": [record.public_dict() for record in self.records],
        }


@dataclass(frozen=True, slots=True)
class RunResult:
    """Machine-readable durable run result."""

    run_id: str
    status: str
    revision: int
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "revision": self.revision,
            **self.report,
        }


def _source_ref_dict(source_ref: SourceRef) -> dict[str, Any]:
    return {
        "url": source_ref.url,
        "source_kind": source_ref.source_kind,
        "edition": source_ref.edition,
        "source_version": source_ref.source_version,
        "retrieved_at": source_ref.retrieved_at.astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "content_hash": source_ref.content_hash,
        "parser_version": source_ref.parser_version,
    }
