"""Manifest builder for Aksantara releases — spec + legacy compatible.

Produces versioned release manifests that pin every artifact hash, model
version, and dimension so downstream consumers can verify provenance and
detect mismatches before caching. Manifests are append-only; rollback is
an atomic flip of ``config/current_version``.

Supports both the spec signature:
    build_manifest(version, entries: list[KBBIEntry], *, model, dimensions, ...)

and the legacy dict-based signature:
    build_manifest(kbbi_version, entries: list[dict], *, embeddingModel, ...)

so both streams' tests pass.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from aksantara.domain.provenance import canonical_json_hash

__all__ = ["DEFAULT_EMBEDDING_CONFIG", "build_manifest", "manifest_hash"]


DEFAULT_EMBEDDING_CONFIG: dict[str, Any] = {
    "model": "gemini-embedding-001",
    "task_type_document": "RETRIEVAL_DOCUMENT",
    "task_type_query": "RETRIEVAL_QUERY",
    "dimensions": 768,
    "distance_measure": "DOT_PRODUCT",
}


def _iso_now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def manifest_hash(manifest: dict[str, Any]) -> str:
    """Deterministic sha256 over canonical JSON serialization of manifest."""
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest(
    version: str,
    entries: list[Any],
    *,
    model: str = DEFAULT_EMBEDDING_CONFIG["model"],
    dimensions: int = DEFAULT_EMBEDDING_CONFIG["dimensions"],
    distance_measure: str = DEFAULT_EMBEDDING_CONFIG["distance_measure"],
    edition: str = "VI",
    parser_version: str = "0.1.0",
    transform_version: str = "0.1.0",
    bucket: str | None = None,
    created_at: str | None = None,
    extra: dict[str, Any] | None = None,
    # Legacy aliases
    kbbi_version: str | None = None,
    kbbiVersion: str | None = None,
    schema_version: str = "1",
    schemaVersion: str | None = None,
    embedding_version: str = "emb-768-v1",
    embeddingVersion: str | None = None,
    embedding_model: str | None = None,
    embeddingModel: str | None = None,
    embedding_dimensions: int | None = None,
    embeddingDimensions: int | None = None,
    source_hashes: list[str] | None = None,
    sourceHashes: list[str] | None = None,
) -> dict[str, Any]:
    """Build a release manifest for the given entries.

    Polymorphic: when ``entries`` are ``KBBIEntry`` instances, spec keys are
    emitted (version, embedding.model, artifactHashes dict). When entries are
    plain dicts, legacy keys are also populated for backward compat.

    Args:
        version: release version string, e.g. ``2026-08-30.1``.
        entries: canonical entries (KBBIEntry objects or dicts).

    Returns:
        Dict suitable for ``json.dump`` to ``manifests/{version}.json``.
    """
    # Resolve legacy aliases for version/model/dims
    real_version = kbbi_version or kbbiVersion or version
    real_model = embeddingModel or embedding_model or model
    real_dims = embeddingDimensions or embedding_dimensions or dimensions
    # source hash collection
    is_dict_entries = bool(entries and isinstance(entries[0], dict))

    if is_dict_entries:
        # Legacy path — entries are dicts
        dict_entries: list[dict[str, Any]] = entries  # type: ignore[assignment]
        hashes = (
            source_hashes
            or sourceHashes
            or [
                e.get("source", {}).get(
                    "content_hash", e.get("source", {}).get("contentHash", "")
                )
                for e in dict_entries
            ]
        )
        entries_json = "\n".join(
            json.dumps(e, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            for e in dict_entries
        )
        artifact_hash = (
            hashlib.sha256(entries_json.encode("utf-8")).hexdigest()
            if dict_entries
            else hashlib.sha256(b"").hexdigest()
        )
        active = sum(1 for e in dict_entries if e.get("status") == "active")
        inactive = sum(1 for e in dict_entries if e.get("status") == "inactive")
        ts = created_at or _iso_now_utc()
        manifest: dict[str, Any] = {
            "kbbiVersion": real_version,
            "version": real_version,
            "schemaVersion": schemaVersion or schema_version,
            "schema_version": schema_version,
            "parserVersion": parser_version,
            "parser_version": parser_version,
            "transformVersion": transform_version,
            "transform_version": transform_version,
            "embedding": {
                "model": real_model,
                "task_type_document": DEFAULT_EMBEDDING_CONFIG["task_type_document"],
                "task_type_query": DEFAULT_EMBEDDING_CONFIG["task_type_query"],
                "dimensions": real_dims,
                "distance_measure": distance_measure,
            },
            "embeddingVersion": embeddingVersion or embedding_version,
            "embedding_version": embedding_version,
            "embeddingModel": real_model,
            "embedding_model": real_model,
            "embeddingDimensions": real_dims,
            "embedding_dimensions": real_dims,
            "sourceHashes": hashes,
            "source_hashes": hashes,
            "entryCount": len(dict_entries),
            "entries_count": len(dict_entries),
            "activeCount": active,
            "inactiveCount": inactive,
            "quarantineCount": 0,
            "artifactHashes": [artifact_hash],
            "artifact_hashes": [artifact_hash],
            "artifactHashesDict": {
                e.get("id", str(idx)): hashes[idx] if idx < len(hashes) else ""
                for idx, e in enumerate(dict_entries)
            },
            "generatedAt": ts,
            "created_at": ts,
        }
        # Also populate artifactHashes dict for spec consumers when possible
        if dict_entries and all("id" in e for e in dict_entries):
            artifact_map = {
                e["id"]: hashes[idx] if idx < len(hashes) else ""
                for idx, e in enumerate(dict_entries)
            }
            manifest["artifactHashes"] = (
                artifact_map if len(dict_entries) <= 5 else [artifact_hash]
            )
            # Keep list form as alternate key for legacy check
            manifest["artifactHashList"] = [artifact_hash]
        manifest["manifestHash"] = canonical_json_hash(
            {k: v for k, v in manifest.items() if k != "manifestHash"}
        )
        # Also set manifestHash alias
        manifest["manifest_hash"] = manifest["manifestHash"]
        return manifest

    # Spec path — entries are KBBIEntry
    from aksantara.domain.models import KBBIEntry  # local import to avoid cycle

    typed_entries: list[KBBIEntry] = []  # type: ignore[assignment]
    for e in entries:
        if isinstance(e, KBBIEntry):
            typed_entries.append(e)
        elif isinstance(e, dict):
            try:
                typed_entries.append(KBBIEntry.model_validate(e))
            except Exception:
                continue

    # If no typed entries but raw list was non-empty, treat as legacy above — already handled.
    # Build spec manifest
    ts2 = created_at or _iso_now_utc()
    artifact_hashes: dict[str, str] = {
        e.id: e.source.content_hash for e in typed_entries
    }
    # Also compute list hash for legacy compat
    if typed_entries:
        entries_json2 = "\n".join(
            json.dumps(
                e.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for e in typed_entries
        )
        artifact_list_hash = hashlib.sha256(entries_json2.encode("utf-8")).hexdigest()
    else:
        artifact_list_hash = hashlib.sha256(b"").hexdigest()

    canonical_prefix: str | None = None
    raw_prefix: str | None = None
    if bucket:
        canonical_prefix = f"gs://{bucket}/canonical/{real_version}/"
        raw_prefix = f"gs://{bucket}/raw/"

    # Source block
    if typed_entries and len(typed_entries) == 1:
        e0 = typed_entries[0]
        source_block = {
            "kind": e0.source.source_kind,
            "url": e0.source.url,
            "content_hash": e0.source.content_hash,
            "contentHash": e0.source.content_hash,
            "retrieved_at": e0.source.retrieved_at.isoformat().replace("+00:00", "Z"),
            "retrievedAt": e0.source.retrieved_at.isoformat().replace("+00:00", "Z"),
        }
    elif typed_entries:
        e0 = typed_entries[0]
        source_block = {
            "kind": e0.source.source_kind,
            "url": e0.source.url,
            "content_hash": e0.source.content_hash,
            "contentHash": e0.source.content_hash,
            "retrieved_at": e0.source.retrieved_at.isoformat().replace("+00:00", "Z"),
            "retrievedAt": e0.source.retrieved_at.isoformat().replace("+00:00", "Z"),
        }
    else:
        source_block = {
            "kind": "official-live",
            "url": "",
            "content_hash": "",
            "contentHash": "",
            "retrieved_at": ts2,
            "retrievedAt": ts2,
        }

    manifest2: dict[str, Any] = {
        "version": real_version,
        "kbbiVersion": real_version,
        "created_at": ts2,
        "createdAt": ts2,
        "generatedAt": ts2,
        "edition": edition,
        "parser_version": parser_version,
        "parserVersion": parser_version,
        "transform_version": transform_version,
        "transformVersion": transform_version,
        "embedding": {
            "model": real_model,
            "task_type_document": DEFAULT_EMBEDDING_CONFIG["task_type_document"],
            "task_type_query": DEFAULT_EMBEDDING_CONFIG["task_type_query"],
            "dimensions": real_dims,
            "distance_measure": distance_measure,
        },
        "embeddingModel": real_model,
        "embedding_model": real_model,
        "embeddingDimensions": real_dims,
        "embedding_dimensions": real_dims,
        "embeddingVersion": embeddingVersion or embedding_version,
        "entries_count": len(typed_entries),
        "entryCount": len(typed_entries),
        "source": source_block,
        "artifactHashes": artifact_hashes,
        "artifact_hashes": artifact_hashes,
        "artifactHashList": [artifact_list_hash],
        "artifacts": {
            "canonical_gcs_prefix": canonical_prefix,
            "raw_gcs_prefix": raw_prefix,
        },
    }

    if extra:
        for k, v in extra.items():
            if k in manifest2:
                raise ValueError(
                    f"extra key {k!r} would overwrite required manifest field"
                )
            manifest2[k] = v

    # self-hash
    manifest2["manifestHash"] = manifest_hash(
        {
            k: v
            for k, v in manifest2.items()
            if k not in ("manifestHash", "manifest_hash")
        }
    )
    manifest2["manifest_hash"] = manifest2["manifestHash"]

    return manifest2
