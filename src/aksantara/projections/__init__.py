"""Projections — generic word/relations downstream manifests."""

from aksantara.embeddings.manifests import build_manifest
from aksantara.projections.generator import (
    artifact_bytes,
    artifact_hash,
    build_artifact_for_track,
    build_relations_artifact,
    build_word_artifact,
)
from aksantara.projections.manifest import (
    build_projection_manifest,
    manifest_self_hash,
    projection_identity,
)
from aksantara.projections.registry import (
    ALLOWED_CONSUMERS,
    ALLOWED_TRACKS,
    GENERATOR_VERSION,
    REJECTED_PRODUCT_IDENTIFIERS,
    SCHEMA_VERSIONS,
    registry_snapshot,
)
from aksantara.projections.schemas import (
    RELATIONS_SCHEMA_V1,
    WORD_SCHEMA_V1,
)
from aksantara.projections.store import (
    ProjectionError,
    generate_projection,
    list_projections,
    projection_manifest_path,
    projection_output_path,
    read_projection_artifact,
    read_projection_manifest,
)

__all__ = [
    "ALLOWED_CONSUMERS",
    "ALLOWED_TRACKS",
    "GENERATOR_VERSION",
    "REJECTED_PRODUCT_IDENTIFIERS",
    "RELATIONS_SCHEMA_V1",
    "SCHEMA_VERSIONS",
    "WORD_SCHEMA_V1",
    "ProjectionError",
    "artifact_bytes",
    "artifact_hash",
    "build_artifact_for_track",
    "build_manifest",
    "build_projection_manifest",
    "build_relations_artifact",
    "build_word_artifact",
    "generate_projection",
    "list_projections",
    "manifest_self_hash",
    "projection_identity",
    "projection_manifest_path",
    "projection_output_path",
    "read_projection_artifact",
    "read_projection_manifest",
    "registry_snapshot",
]
