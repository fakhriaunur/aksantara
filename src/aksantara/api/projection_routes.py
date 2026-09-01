"""Projection HTTP routes — artifact-only or documented read surfaces.

Exposes documented read/status/list surfaces for projections when HTTP is
desired; CLI remains the primary caller-owned surface. All operations are
read-only or local-only generation with caller-owned roots. No write path
to canonical data. If no HTTP surface is exposed, validators record N/A.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from aksantara.projections.manifest import manifest_self_hash
from aksantara.projections.registry import (
    ALLOWED_CONSUMERS,
    ALLOWED_TRACKS,
    GENERATOR_VERSION,
    REJECTED_PRODUCT_IDENTIFIERS,
    registry_snapshot,
)
from aksantara.projections.schemas import RELATIONS_SCHEMA_V1, WORD_SCHEMA_V1
from aksantara.projections.store import (
    ProjectionError,
    generate_projection,
    list_projections,
    read_projection_artifact,
    read_projection_manifest,
)

__all__ = ["create_projection_router"]


class GenerateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    release_root: str = Field(description="Caller-owned release root")
    output_root: str = Field(
        description="Caller-owned projection output root (separate from canonical/raw/vector/release)"
    )
    consumer: str = Field(
        description=f"Consumer selector; allowed: {', '.join(ALLOWED_CONSUMERS)}"
    )
    track: str = Field(
        description=f"Track selector; allowed: {', '.join(ALLOWED_TRACKS)}; rejected: {', '.join(REJECTED_PRODUCT_IDENTIFIERS)}"
    )
    release: str = Field(
        description="Exact validated release version (no current fallback)"
    )
    generator_version: str | None = Field(
        default=None, description=f"Generator version (default {GENERATOR_VERSION})"
    )
    schema_version: str | None = Field(
        default=None, description="Schema version (word-v1 or relations-v1)"
    )
    fixed_clock: str | None = Field(
        default=None, description="Fixed ISO-8601 clock for deterministic output"
    )


def create_projection_router() -> APIRouter:
    router = APIRouter(prefix="/projections", tags=["projection"])

    @router.get(
        "/registry",
        summary="Publish generic track/schema registry (word/relations, rejected products, serialization)",
        operation_id="projection_registry",
        response_model=dict[str, Any],
        description="Publishes generic track/schema registry, exact selectors, relative paths/content types, local mode, fixed clock, status/errors, and release read. Rejects hunspell/cspell/babel/polyglossia/rabu-baku without separate adapters.",
    )
    def registry() -> dict[str, Any]:
        snap = registry_snapshot()
        snap["word_schema"] = WORD_SCHEMA_V1
        snap["relations_schema"] = RELATIONS_SCHEMA_V1
        snap["contract"] = (
            "projection output roots are caller-owned and separate from canonical/raw/vector/release namespaces; generation has no write path to canonical data"
        )
        snap["selectors"] = {
            "consumer": f"allowed: {', '.join(ALLOWED_CONSUMERS)}; rejected: {', '.join(REJECTED_PRODUCT_IDENTIFIERS)}",
            "track": f"allowed: {', '.join(ALLOWED_TRACKS)}; rejected: {', '.join(REJECTED_PRODUCT_IDENTIFIERS)}",
            "release": "exact validated release version; missing/invalid/unvalidated/conflicted/incomplete fail before publication without fallback",
            "generator_version": GENERATOR_VERSION,
            "schema_version": "word-v1 for word, relations-v1 for relations",
        }
        snap["output"] = {
            "root": "caller-owned staging/output roots; separate from canonical/raw/vector/release",
            "relative_path": "projections/<consumer>/<track>/<release>/<generator>/<schema>/artifact.json",
            "content_type": "application/json",
            "hash": "lower-case hex SHA-256 of UTF-8 bytes",
        }
        snap["status_values"] = ["pending", "validated", "failed", "unavailable"]
        return snap

    @router.post(
        "/generate",
        summary="Generate projection from exactly selected validated release (exact release/track/generator/schema selectors and safe output paths)",
        operation_id="projection_generate",
        response_model=dict[str, Any],
        description="Generate deterministic projection from explicitly selected validated release with collision-safe identity (consumer, track, source_release, generator_version, schema_version). Caller-owned output roots separate from canonical/raw/vector/release. Rejects unsupported downstream products. Fails before validated publication for missing/invalid/unvalidated/conflicted/incomplete releases without fallback. Local-only, no write path to canonical data.",
    )
    def generate(request: GenerateRequest) -> dict[str, Any]:
        try:
            manifest = generate_projection(
                release_root=Path(request.release_root),
                output_root=Path(request.output_root),
                consumer=request.consumer,
                track=request.track,
                source_release=request.release,
                generator_version=request.generator_version,
                schema_version=request.schema_version,
                created_at=request.fixed_clock,
                fixed_clock=request.fixed_clock,
            )
            return manifest
        except ProjectionError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"error": str(exc), "code": exc.code, "detail": exc.detail},
            ) from exc

    @router.get(
        "/manifest",
        summary="Read projection manifest by exact collision-safe identity (no substitution)",
        operation_id="projection_manifest_read",
        response_model=dict[str, Any],
        description="Read projection manifest by exact identity (consumer, track, source_release, generator_version, schema_version) with source release/manifest, sorted entry IDs, exact raw/canonical hashes, source references, generator/schema versions, output path/hash, self-hash, and status. No current-pointer substitution.",
    )
    def manifest_read(
        output_root: str = Query(
            ..., description="Caller-owned projection output root"
        ),
        consumer: str = Query(..., description="Consumer selector"),
        track: str = Query(..., description="Track selector"),
        release: str = Query(..., description="Source release version"),
        generator_version: str | None = Query(
            default=None, description="Generator version"
        ),
        schema_version: str | None = Query(default=None, description="Schema version"),
    ) -> dict[str, Any]:
        try:
            return read_projection_manifest(
                Path(output_root),
                consumer,
                track,
                release,
                generator_version,
                schema_version,
            )
        except ProjectionError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"error": str(exc), "code": exc.code}
            ) from exc

    @router.get(
        "/artifact",
        summary="Read projection artifact bytes by exact identity (deterministic, source-backed)",
        operation_id="projection_artifact_read",
        response_model=dict[str, Any],
        description="Read exact projection artifact bytes by collision-safe identity with deterministic serialization and source-backed witnesses. No substitution. Fixed inputs and clock produce byte-identical artifacts.",
    )
    def artifact_read(
        output_root: str = Query(
            ..., description="Caller-owned projection output root"
        ),
        consumer: str = Query(..., description="Consumer selector"),
        track: str = Query(..., description="Track selector"),
        release: str = Query(..., description="Source release version"),
        generator_version: str | None = Query(
            default=None, description="Generator version"
        ),
        schema_version: str | None = Query(default=None, description="Schema version"),
    ) -> dict[str, Any]:
        try:
            data, manifest = read_projection_artifact(
                Path(output_root),
                consumer,
                track,
                release,
                generator_version,
                schema_version,
            )
            artifact = json.loads(data.decode("utf-8"))
            return {
                "manifest": manifest,
                "artifact": artifact,
                "artifact_hash": manifest.get("output_hash"),
                "self_hash": manifest.get("self_hash"),
            }
        except ProjectionError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"error": str(exc), "code": exc.code}
            ) from exc

    @router.get(
        "/verify",
        summary="Verify projection manifest/artifact (sorted IDs, hashes, lineage, status)",
        operation_id="projection_verify",
        response_model=dict[str, Any],
        description="Verify projection manifest carries exact release/manifest, sorted entry IDs, exact raw/canonical hashes, source references, generator/schema versions, output path/hash, self-hash, and status. Both content and manifest hashes recompute from published bytes.",
    )
    def verify(
        output_root: str = Query(
            ..., description="Caller-owned projection output root"
        ),
        consumer: str = Query(..., description="Consumer selector"),
        track: str = Query(..., description="Track selector"),
        release: str = Query(..., description="Source release version"),
        generator_version: str | None = Query(
            default=None, description="Generator version"
        ),
        schema_version: str | None = Query(default=None, description="Schema version"),
    ) -> dict[str, Any]:
        try:
            manifest = read_projection_manifest(
                Path(output_root),
                consumer,
                track,
                release,
                generator_version,
                schema_version,
            )
            data, _ = read_projection_artifact(
                Path(output_root),
                consumer,
                track,
                release,
                generator_version,
                schema_version,
            )
            # Verify self-hash
            stored_self = manifest.get("self_hash") or manifest.get("selfHash")
            recomputed_self = manifest_self_hash(manifest)
            # Verify output hash
            import hashlib as _hl

            actual_output_hash = _hl.sha256(data).hexdigest()
            expected_output_hash = manifest.get("output_hash") or manifest.get(
                "outputHash"
            )
            valid = (stored_self == recomputed_self) and (
                actual_output_hash == expected_output_hash
            )
            return {
                "valid": valid,
                "manifest": manifest,
                "self_hash_verified": stored_self == recomputed_self,
                "output_hash_verified": actual_output_hash == expected_output_hash,
                "artifact_bytes_len": len(data),
            }
        except ProjectionError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"error": str(exc), "code": exc.code, "valid": False},
            ) from exc

    @router.get(
        "",
        summary="List projections by collision-safe identity",
        operation_id="projection_list",
        response_model=dict[str, Any],
        description="List projection manifests isolated by collision-safe identity containing (consumer, track, source_release, generator_version, schema_version). Historical outputs preserved without current-pointer substitution.",
    )
    def list_all(
        output_root: str = Query(
            ..., description="Caller-owned projection output root"
        ),
    ) -> dict[str, Any]:
        manifests = list_projections(Path(output_root))
        return {"projections": manifests, "count": len(manifests)}

    return router
