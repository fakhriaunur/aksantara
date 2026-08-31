"""Public read-only deterministic snapshot replay endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aksantara.domain.models import SourceRef
from aksantara.ingest.public_replay import (
    TRANSFORM_VERSION,
    VALIDATION_POLICY_VERSION,
    ReplayError,
    replay_snapshot,
)
from aksantara.parse.parser_contract import PARSER_VERSION

__all__ = ["ReplayRequest", "create_replay_router"]


class ReplayRequest(BaseModel):
    """Strict request for one immutable source snapshot replay."""

    model_config = ConfigDict(strict=True, extra="forbid")

    root: str = Field(
        min_length=1,
        description="Caller-owned root containing the raw snapshot",
    )
    raw_path: str = Field(
        min_length=1,
        description="Raw snapshot path, absolute or relative to root",
    )
    source_ref: dict[str, Any] = Field(
        description="Immutable official KBBI provenance reference",
    )
    expected_raw_hash: str = Field(
        min_length=64,
        max_length=64,
        description="Expected SHA-256 of the raw snapshot bytes",
    )
    expected_canonical_hash: str | None = Field(
        default=None,
        description="Optional expected SHA-256 of canonical published bytes",
    )
    stable_key: str | None = Field(
        default=None,
        description="Expected normalized entry key; inferred from source URL when omitted",
    )
    parser_version: str = Field(default=PARSER_VERSION, min_length=1)
    transform_version: str = Field(default=TRANSFORM_VERSION, min_length=1)
    validation_policy: str = Field(default=VALIDATION_POLICY_VERSION, min_length=1)


def create_replay_router() -> APIRouter:
    """Create the public replay router without stateful dependencies."""
    router = APIRouter(tags=["replay"])

    @router.post(
        "/replay",
        summary="Replay one KBBI snapshot deterministically",
        operation_id="replay_snapshot",
        response_model=dict[str, Any],
        description=(
            "Read-only local replay. Verifies raw bytes and SourceRef before "
            "deterministic parsing, then returns canonical serialization. "
            "Never fetches live data, invokes an LLM, repairs state, or writes "
            "canonical, candidate, release, or pointer artifacts."
        ),
    )
    def replay(request: ReplayRequest) -> dict[str, Any] | JSONResponse:
        try:
            source_payload = dict(request.source_ref)
            retrieved_at = source_payload.get("retrieved_at")
            if isinstance(retrieved_at, str):
                try:
                    parsed_time = datetime.fromisoformat(
                        retrieved_at.replace("Z", "+00:00")
                    )
                except (TypeError, ValueError) as exc:
                    raise ReplayError(
                        "replay_source_ref_invalid",
                        "source reference is invalid",
                        details={"error_type": type(exc).__name__},
                    ) from exc
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=UTC)
                source_payload["retrieved_at"] = parsed_time.astimezone(UTC)
            try:
                source_ref = SourceRef.model_validate(source_payload)
            except (TypeError, ValueError) as exc:
                raise ReplayError(
                    "replay_source_ref_invalid",
                    "source reference is invalid",
                    details={"error_type": type(exc).__name__},
                ) from exc
            return replay_snapshot(
                root=Path(request.root).expanduser().resolve(),
                raw_path=request.raw_path,
                source_ref=source_ref,
                expected_raw_hash=request.expected_raw_hash,
                expected_canonical_hash=request.expected_canonical_hash,
                stable_key=request.stable_key,
                parser_version=request.parser_version,
                transform_version=request.transform_version,
                validation_policy=request.validation_policy,
            )
        except ReplayError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.to_dict()},
            )

    return router
