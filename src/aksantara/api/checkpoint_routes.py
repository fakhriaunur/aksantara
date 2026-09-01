"""Public checkpoint lifecycle routes.

These routes are intentionally local-only.  A request must name a caller-owned
root and either a catalog path under that root or an inline JSON catalog whose
fixture bindings are still validated by :mod:`aksantara.ingest.checkpoint`.
There is no cloud client, network transport, release promotion, or pointer
mutation in this router.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from aksantara.domain.errors import AksantaraDomainError
from aksantara.ingest.checkpoint import (
    CatalogValidationError,
    CheckpointDriver,
    CheckpointError,
    CheckpointNotFoundError,
)
from aksantara.validate.review import (
    ReviewDecisionConflictError,
    ReviewError,
    ReviewNotFoundError,
)

__all__ = [
    "CandidateEvaluationRequest",
    "CheckpointCreateRequest",
    "ReviewDecisionRequest",
    "create_checkpoint_router",
]


class CheckpointCreateRequest(BaseModel):
    """Strict request for a caller-owned local checkpoint."""

    model_config = ConfigDict(strict=True, extra="forbid")

    root: str = Field(
        min_length=1,
        description="Absolute or process-relative caller-owned artifact root",
    )
    catalog_path: str | None = Field(
        default=None,
        description="JSON catalog path, required when catalog is omitted; must be under root",
    )
    catalog: dict[str, Any] | None = Field(
        default=None,
        description="Inline catalog manifest; fixture paths remain relative to root",
    )
    limit: int | str | None = Field(
        default=None,
        description="Integer 1..100 inclusive; defaults to 100; no coercion or padding",
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Caller key scoped to root and the complete run tuple",
    )
    mode: str = Field(
        default="local-fixture-only",
        description="Only local-fixture-only is supported; cloud mode is rejected",
    )


class ReviewDecisionRequest(BaseModel):
    """Explicit local operator decision for one review record."""

    model_config = ConfigDict(strict=True, extra="forbid")

    root: str = Field(
        min_length=1,
        description="Caller-owned root containing the durable review record",
    )
    decision: Literal["select_official", "block", "reject"] = Field(
        description="One of select_official, block, or reject",
    )
    reviewer: str = Field(min_length=1, description="Human reviewer identity")
    reason: str = Field(min_length=1, description="Reason retained in history")
    policy_version: str = Field(
        min_length=1,
        description="Policy pin used for this decision",
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Repeat-safe decision key",
    )
    timestamp: str | None = Field(
        default=None,
        description="Optional fixed ISO-8601 timestamp for deterministic fixtures",
    )


class CandidateEvaluationRequest(BaseModel):
    """Candidate gate request, including optional release-level approval."""

    model_config = ConfigDict(strict=True, extra="forbid")

    root: str = Field(
        min_length=1,
        description="Caller-owned root containing the checkpoint artifacts",
    )
    release_approved: bool = Field(
        default=False,
        description="Explicit release-level human approval; never implicit",
    )
    release_reviewer: str | None = Field(
        default=None,
        description="Human approver identity",
    )
    release_reason: str | None = Field(
        default=None,
        description="Reason retained with the candidate approval",
    )


_drivers: dict[tuple[str, str], CheckpointDriver] = {}
_drivers_lock = threading.RLock()


def _error_response(error: CheckpointError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.to_dict())


def _review_error_response(error: ReviewError) -> HTTPException:
    if isinstance(error, ReviewNotFoundError):
        status_code = 404
        code = "review_not_found"
    elif isinstance(error, ReviewDecisionConflictError):
        status_code = 409
        code = "review_decision_conflict"
    else:
        status_code = 422
        code = "invalid_review_request"
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": str(error)},
    )


def _load_catalog(request: CheckpointCreateRequest, root: Path) -> dict[str, Any]:
    if request.mode != "local-fixture-only":
        raise CatalogValidationError(
            "checkpoint API only supports local-fixture-only mode",
            details={"mode": request.mode},
        )
    if (request.catalog is None) == (request.catalog_path is None):
        raise CatalogValidationError(
            "provide exactly one of catalog or catalog_path",
            details={
                "catalog": request.catalog is not None,
                "catalog_path": request.catalog_path is not None,
            },
        )
    if request.catalog is not None:
        return request.catalog
    assert request.catalog_path is not None
    candidate = Path(request.catalog_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise CatalogValidationError(
            "catalog_path escapes caller root",
            details={"catalog_path": request.catalog_path, "root": str(root)},
        ) from exc
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogValidationError(
            "catalog_path does not exist",
            details={"catalog_path": request.catalog_path},
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError(
            "catalog_path is not valid UTF-8 JSON",
            details={
                "catalog_path": request.catalog_path,
                "error_type": type(exc).__name__,
            },
        ) from exc
    if not isinstance(value, dict):
        raise CatalogValidationError("catalog_path must contain a JSON object")
    return value


def _driver_for(run_id: str, root: str | None) -> CheckpointDriver:
    if root is not None:
        driver = CheckpointDriver(root=Path(root).expanduser().resolve())
        with _drivers_lock:
            _drivers[(run_id, str(driver.root))] = driver
        return driver
    with _drivers_lock:
        candidates = [
            driver
            for (cached_run_id, _), driver in _drivers.items()
            if cached_run_id == run_id
        ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise CheckpointNotFoundError(
            "run root is required when the run id exists in multiple roots",
            details={
                "run_id": run_id,
                "hint": "supply the caller-owned root query parameter",
            },
        )
    if not candidates:
        raise CheckpointNotFoundError(
            "run root is required after process restart",
            details={
                "run_id": run_id,
                "hint": "supply the caller-owned root query parameter",
            },
        )
    return candidates[0]


def create_checkpoint_router() -> APIRouter:
    """Create the documented checkpoint lifecycle router."""
    router = APIRouter(prefix="/checkpoints", tags=["checkpoint"])

    @router.get(
        "/contract",
        summary="Describe checkpoint contract",
        operation_id="checkpoint_contract",
        response_model=dict[str, Any],
    )
    def contract() -> dict[str, Any]:
        """Return normalization, limits, identities, and safety mappings."""
        return CheckpointDriver.contract()

    @router.post(
        "/runs",
        summary="Create and execute local checkpoint",
        operation_id="checkpoint_create_run",
        status_code=201,
        response_model=dict[str, Any],
        description=(
            "Local-only mutation. Requires a caller-owned root and fixture "
            "catalog. Preflight validates identity, paths, hosts, hashes, and "
            "limit before any fixture read. It never promotes a release."
        ),
    )
    def create_run(request: CheckpointCreateRequest) -> dict[str, Any]:
        try:
            root = Path(request.root).expanduser().resolve()
            driver = CheckpointDriver(root=root)
            catalog = _load_catalog(request, root)
            result = driver.run(
                catalog,
                limit=request.limit,
                idempotency_key=request.idempotency_key,
            )
        except CheckpointError as exc:
            raise _error_response(exc) from exc
        with _drivers_lock:
            _drivers[(result.run_id, str(driver.root))] = driver
        return result.to_dict()

    @router.post(
        "/runs/{run_id}/execute",
        summary="Read an existing checkpoint execution",
        operation_id="checkpoint_execute_run",
        response_model=dict[str, Any],
        description=(
            "Local-only idempotent no-op. It reads the durable result and "
            "cannot fetch, retry, promote, or change the pointer."
        ),
    )
    def execute_run(
        run_id: str,
        root: str | None = Query(
            default=None,
            description="Caller-owned root; required when the API process was restarted",
        ),
    ) -> dict[str, Any]:
        try:
            return _driver_for(run_id, root).execute(run_id).to_dict()
        except CheckpointError as exc:
            raise _error_response(exc) from exc

    @router.get(
        "/runs/history",
        summary="Read immutable checkpoint run history",
        operation_id="checkpoint_run_history",
        response_model=dict[str, Any],
        description=(
            "Read-only list of durable runs and report revisions under a "
            "caller-owned root. Changed source fingerprints create a new "
            "run and never rewrite a prior report."
        ),
    )
    def run_history(
        root: str = Query(..., description="Caller-owned checkpoint artifact root"),
    ) -> dict[str, Any]:
        try:
            return CheckpointDriver(root=Path(root).expanduser().resolve()).history()
        except CheckpointError as exc:
            raise _error_response(exc) from exc

    @router.get(
        "/runs/{run_id}",
        summary="Read checkpoint status",
        operation_id="checkpoint_run_status",
        response_model=dict[str, Any],
    )
    def run_status(
        run_id: str,
        root: str | None = Query(
            default=None,
            description="Caller-owned root; required after process restart",
        ),
    ) -> dict[str, Any]:
        try:
            return _driver_for(run_id, root).status(run_id)
        except CheckpointError as exc:
            raise _error_response(exc) from exc

    @router.get(
        "/runs/{run_id}/report",
        summary="Read conserved checkpoint report",
        operation_id="checkpoint_run_report",
        response_model=dict[str, Any],
    )
    def run_report(
        run_id: str,
        root: str | None = Query(default=None, description="Caller-owned root"),
    ) -> dict[str, Any]:
        try:
            return _driver_for(run_id, root).report(run_id)
        except CheckpointError as exc:
            raise _error_response(exc) from exc

    @router.get(
        "/runs/{run_id}/outcomes",
        summary="Read current checkpoint outcomes",
        operation_id="checkpoint_current_outcomes",
        response_model=dict[str, Any],
    )
    def current_outcomes(
        run_id: str,
        root: str | None = Query(default=None, description="Caller-owned root"),
    ) -> dict[str, Any]:
        try:
            return _driver_for(run_id, root).current_outcomes(run_id)
        except CheckpointError as exc:
            raise _error_response(exc) from exc

    @router.get(
        "/runs/{run_id}/attempts",
        summary="Read checkpoint attempt history",
        operation_id="checkpoint_attempt_history",
        response_model=dict[str, Any],
    )
    def attempt_history(
        run_id: str,
        root: str | None = Query(default=None, description="Caller-owned root"),
    ) -> dict[str, Any]:
        try:
            return _driver_for(run_id, root).attempts(run_id)
        except CheckpointError as exc:
            raise _error_response(exc) from exc

    @router.get(
        "/runs/{run_id}/checkpoint",
        summary="Read durable checkpoint reference",
        operation_id="checkpoint_revision",
        response_model=dict[str, Any],
    )
    def checkpoint_revision(
        run_id: str,
        root: str | None = Query(default=None, description="Caller-owned root"),
    ) -> dict[str, Any]:
        try:
            return _driver_for(run_id, root).checkpoint(run_id)
        except CheckpointError as exc:
            raise _error_response(exc) from exc

    @router.get(
        "/reviews",
        summary="Read deterministic open authority review queue",
        operation_id="checkpoint_review_queue",
        response_model=dict[str, Any],
        description=(
            "Local-only read from a caller-owned root. Results are sorted by "
            "stable key and review ID and include immutable source evidence."
        ),
    )
    def review_queue(
        root: str = Query(..., description="Caller-owned review artifact root"),
    ) -> dict[str, Any]:
        try:
            records = CheckpointDriver(
                root=Path(root).expanduser().resolve()
            ).review_queue()
            return {
                "schema_version": "authority-review-v1",
                "count": len(records),
                "reviews": records,
            }
        except CheckpointError as exc:
            raise _error_response(exc) from exc
        except ReviewError as exc:
            raise _review_error_response(exc) from exc

    @router.get(
        "/reviews/{review_id}",
        summary="Read one authority review record",
        operation_id="checkpoint_review_read",
        response_model=dict[str, Any],
    )
    def review_read(
        review_id: str,
        root: str = Query(..., description="Caller-owned review artifact root"),
    ) -> dict[str, Any]:
        try:
            return CheckpointDriver(root=Path(root).expanduser().resolve()).review_read(
                review_id
            )
        except CheckpointError as exc:
            raise _error_response(exc) from exc
        except ReviewError as exc:
            raise _review_error_response(exc) from exc

    @router.post(
        "/reviews/{review_id}/decisions",
        summary="Append an explicit authority review decision",
        operation_id="checkpoint_review_decision",
        response_model=dict[str, Any],
        description=(
            "Local-only mutation in a caller-owned root. Decisions are "
            "append-only and idempotent by idempotency_key. select_official "
            "never selects a fallback source."
        ),
    )
    def review_decision(
        review_id: str,
        request: ReviewDecisionRequest,
    ) -> dict[str, Any]:
        try:
            return CheckpointDriver(
                root=Path(request.root).expanduser().resolve()
            ).review_decide(
                review_id,
                decision=request.decision,
                reviewer=request.reviewer,
                reason=request.reason,
                policy_version=request.policy_version,
                idempotency_key=request.idempotency_key,
                timestamp=request.timestamp,
            )
        except CheckpointError as exc:
            raise _error_response(exc) from exc
        except ReviewError as exc:
            raise _review_error_response(exc) from exc

    @router.post(
        "/runs/{run_id}/candidate",
        summary="Evaluate checkpoint candidate eligibility",
        operation_id="checkpoint_candidate_evaluate",
        response_model=dict[str, Any],
        description=(
            "Local-only fail-closed candidate gate. It requires exact "
            "official source, raw/canonical joins, terminal outcomes, "
            "resolved item reviews, a complete fixed checkpoint, and an "
            "explicit release-level approval. It never changes the current "
            "release pointer or creates vectors."
        ),
    )
    def candidate_evaluate(
        run_id: str,
        request: CandidateEvaluationRequest,
    ) -> dict[str, Any]:
        try:
            return CheckpointDriver(
                root=Path(request.root).expanduser().resolve()
            ).evaluate_candidate(
                run_id,
                release_approved=request.release_approved,
                release_reviewer=request.release_reviewer,
                release_reason=request.release_reason,
            )
        except CheckpointError as exc:
            raise _error_response(exc) from exc
        except AksantaraDomainError as exc:
            # Domain validation errors including QuarantinedError are normalized
            # to typed CheckpointError without traceback or 500.
            mapped = CheckpointError(
                str(exc),
                details={
                    "reason": getattr(exc, "reason", str(exc)),
                    "error_type": type(exc).__name__,
                },
            )
            raise _error_response(mapped) from exc
        except Exception as exc:
            mapped = CheckpointError(
                "candidate evaluation failed",
                details={"error_type": type(exc).__name__},
            )
            raise _error_response(mapped) from exc

    @router.get(
        "/runs/{run_id}/candidate",
        summary="Read candidate eligibility evaluation",
        operation_id="checkpoint_candidate_read",
        response_model=dict[str, Any],
    )
    def candidate_read(
        run_id: str,
        root: str = Query(..., description="Caller-owned checkpoint artifact root"),
    ) -> dict[str, Any]:
        try:
            return CheckpointDriver(
                root=Path(root).expanduser().resolve()
            ).candidate_evaluation(run_id)
        except CheckpointError as exc:
            raise _error_response(exc) from exc
        except AksantaraDomainError as exc:
            mapped = CheckpointError(
                str(exc),
                details={
                    "reason": getattr(exc, "reason", str(exc)),
                    "error_type": type(exc).__name__,
                },
            )
            raise _error_response(mapped) from exc
        except Exception as exc:
            mapped = CheckpointError(
                "candidate read failed",
                details={"error_type": type(exc).__name__},
            )
            raise _error_response(mapped) from exc

    return router
