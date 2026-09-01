"""Projection manifest with exact release/source lineage and deterministic hashes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "build_projection_manifest",
    "manifest_self_hash",
    "projection_identity",
    "validate_projection_manifest",
]


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return (text + "\n").encode("utf-8")


def manifest_self_hash(manifest: dict[str, Any]) -> str:
    """Hash of manifest bytes excluding self_hash field."""
    payload = {
        k: v
        for k, v in manifest.items()
        if k not in ("self_hash", "selfHash", "projection_manifest_hash")
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def projection_identity(
    consumer: str,
    track: str,
    source_release: str,
    generator_version: str,
    schema_version: str,
) -> str:
    """Collision-safe identity: consumer/track/release/generator/schema."""
    # Use explicit delimiter that cannot collide with allowed chars
    # consumer and track are validated to not contain / or :
    return f"{consumer}:{track}:{source_release}:{generator_version}:{schema_version}"


def build_projection_manifest(
    *,
    consumer: str,
    track: str,
    source_release: str,
    source_manifest_hash: str,
    source_entries: list[dict[str, Any]],
    generator_version: str,
    schema_version: str,
    output_path: str,
    output_hash: str,
    created_at: str | None = None,
    status: str = "validated",
) -> dict[str, Any]:
    """Build projection manifest carrying exact release and hash lineage.

    source_entries: list of {id, canonical_content_hash, raw_content_hash, source_url, source_kind, source_release}
                    sorted by id
    """
    sorted_entries = sorted(source_entries, key=lambda e: e["id"])
    sorted_ids = [e["id"] for e in sorted_entries]

    manifest: dict[str, Any] = {
        "consumer": consumer,
        "track": track,
        "source_release": source_release,
        "source_manifest_hash": source_manifest_hash,
        "manifest_hash": source_manifest_hash,
        "generator_version": generator_version,
        "schema_version": schema_version,
        "identity": projection_identity(
            consumer, track, source_release, generator_version, schema_version
        ),
        "source_entries": sorted_entries,
        "sorted_entry_ids": sorted_ids,
        "entry_count": len(sorted_entries),
        "output_path": output_path,
        "output_hash": output_hash,
        "created_at": created_at
        or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": status,
    }
    # Compute self-hash
    manifest["self_hash"] = manifest_self_hash(manifest)
    manifest["selfHash"] = manifest["self_hash"]
    manifest["projection_manifest_hash"] = manifest["self_hash"]
    return manifest


def validate_projection_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = [
        "consumer",
        "track",
        "source_release",
        "source_manifest_hash",
        "generator_version",
        "schema_version",
        "identity",
        "source_entries",
        "output_path",
        "output_hash",
        "self_hash",
        "status",
    ]
    for field in required:
        if (
            field not in manifest
            or manifest[field] is None
            or (
                isinstance(manifest[field], str)
                and not manifest[field].strip()
                and field not in ("output_hash",)
            )
        ):
            # output_hash can be empty for empty release? but should be hash of empty list
            if field == "output_hash" and manifest.get(field) == "":
                continue
            errors.append(f"missing required field: {field}")
    # Check sorted
    entries = manifest.get("source_entries", [])
    if isinstance(entries, list) and len(entries) > 1:
        ids = [e.get("id", "") for e in entries]
        if ids != sorted(ids):
            errors.append("source_entries not sorted by id")
        # duplicate check
        if len(ids) != len(set(ids)):
            errors.append("duplicate entry ids in source_entries")
        # hash format
        for e in entries:
            for hf in ("canonical_content_hash", "raw_content_hash"):
                h = e.get(hf, "")
                if h and (
                    len(h) != 64 or not all(c in "0123456789abcdef" for c in h.lower())
                ):
                    errors.append(f"{hf} for {e.get('id')} must be 64 hex chars")
    # Self-hash recomputation
    if "self_hash" in manifest:
        stored = manifest["self_hash"]
        recomputed = manifest_self_hash(manifest)
        if stored != recomputed:
            errors.append(
                f"self_hash mismatch: stored {stored!r} != recomputed {recomputed!r}"
            )
    # Identity consistency
    if all(
        k in manifest
        for k in (
            "consumer",
            "track",
            "source_release",
            "generator_version",
            "schema_version",
        )
    ):
        expected_id = projection_identity(
            manifest["consumer"],
            manifest["track"],
            manifest["source_release"],
            manifest["generator_version"],
            manifest["schema_version"],
        )
        if manifest.get("identity") != expected_id:
            errors.append(
                f"identity mismatch: {manifest.get('identity')!r} != {expected_id!r}"
            )
    # Status check
    if manifest.get("status") not in ("pending", "validated", "failed", "unavailable"):
        errors.append(f"invalid status: {manifest.get('status')!r}")
    # Output path should be relative
    op = manifest.get("output_path", "")
    if op and (op.startswith("/") or ".." in op):
        errors.append(f"output_path must be relative and not contain traversal: {op!r}")
    return errors
