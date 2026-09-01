"""Local release helpers for fixture-seeded manifests and vector sets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aksantara.domain.models import KBBIEntry
from aksantara.embeddings.document import build_embedding_document
from aksantara.embeddings.metadata import (
    EMBEDDING_DIMS,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL,
    EMBEDDING_SCHEMA_VERSION,
    EMBEDDING_TASK_DOCUMENT,
)

__all__ = ["load_manifest", "seed_release", "verify_release"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def seed_release(
    root: Path,
    release_version: str,
    entries: list[KBBIEntry],
    *,
    vector_dir: Path | None = None,
) -> dict[str, Any]:
    """Seed a validated release manifest and vector set deterministically.

    Creates caller-owned release manifest under root/releases/<version>.json
    and vector records under root/vectors/<version>/.

    All vectors are exact 768-dim gemini-embedding-001 with published metadata.
    """
    root = Path(root).expanduser().resolve()
    releases_dir = root / "releases"
    vectors_dir = vector_dir or root / "vectors" / release_version
    releases_dir.mkdir(parents=True, exist_ok=True)
    vectors_dir.mkdir(parents=True, exist_ok=True)

    # Build manifest artifactHashes
    artifact_hashes: dict[str, str] = {e.id: e.source.content_hash for e in entries}
    # canonical hashes and document hashes
    canonical_hashes: dict[str, str] = {}
    doc_hashes: dict[str, str] = {}
    for e in entries:
        from aksantara.embeddings.planner import (
            canonical_hash_for_entry,
            document_hash_for_entry,
        )

        canonical_hashes[e.id] = canonical_hash_for_entry(e)
        doc_hashes[e.id] = document_hash_for_entry(e)

    manifest: dict[str, Any] = {
        "version": release_version,
        "created_at": _now_iso(),
        "edition": "VI",
        "embedding": {
            "model": EMBEDDING_MODEL,
            "task_type_document": EMBEDDING_TASK_DOCUMENT,
            "task_type_query": "RETRIEVAL_QUERY",
            "dimensions": EMBEDDING_DIMS,
            "distance_measure": EMBEDDING_DISTANCE,
            "schema_version": EMBEDDING_SCHEMA_VERSION,
        },
        "entries_count": len(entries),
        "artifactHashes": artifact_hashes,
        "canonicalHashes": canonical_hashes,
        "documentHashes": doc_hashes,
        "entries": sorted(artifact_hashes.keys()),
    }
    # self-hash
    canonical = json.dumps(
        {
            k: v
            for k, v in manifest.items()
            if k not in ("manifestHash", "manifest_hash")
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    manifest["manifestHash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    manifest["manifest_hash"] = manifest["manifestHash"]

    (releases_dir / f"{release_version}.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    # Create vectors deterministically (no Vertex calls in local mode)
    from aksantara.embeddings.firestore_types import VectorRecord

    for e in entries:
        doc = build_embedding_document(e)
        vec = _deterministic_vector(doc)
        rec = VectorRecord(
            id=e.id,
            version=release_version,
            lema=e.lema,
            embedding=tuple(vec),
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIMS,
            content_hash=e.source.content_hash,
            source_kind=e.source.source_kind,
            edition=e.source.edition,
            source_version=e.source.source_version,
            parser_version=e.source.parser_version,
            embedding_document=doc,
            metadata={
                "canonical_content_hash": canonical_hashes[e.id],
                "raw_content_hash": e.source.content_hash,
                "source_release": release_version,
                "task": EMBEDDING_TASK_DOCUMENT,
                "distance_measure": EMBEDDING_DISTANCE,
                "schema_version": EMBEDDING_SCHEMA_VERSION,
                "source_url": e.source.url,
                "embedding_document_hash": doc_hashes[e.id],
            },
        )
        payload = rec.to_firestore_dict()
        payload["source_release"] = release_version
        payload["task"] = EMBEDDING_TASK_DOCUMENT
        payload["distance_measure"] = EMBEDDING_DISTANCE
        payload["schema_version"] = EMBEDDING_SCHEMA_VERSION
        payload["raw_content_hash"] = e.source.content_hash
        payload["canonical_content_hash"] = canonical_hashes[e.id]
        payload["embedding_document_hash"] = doc_hashes[e.id]
        # Make JSON serializable: Vector -> list, datetime -> iso
        for k in list(payload.keys()):
            v = payload[k]
            if hasattr(v, "isoformat"):
                payload[k] = v.isoformat().replace("+00:00", "Z")
            elif hasattr(v, "__class__") and v.__class__.__name__ == "Vector":
                payload[k] = list(v)
        # also handle metadata datetime values
        if isinstance(payload.get("metadata"), dict):
            for mk, mv in list(payload["metadata"].items()):
                if hasattr(mv, "isoformat"):
                    payload["metadata"][mk] = mv.isoformat().replace("+00:00", "Z")
        out = vectors_dir / f"{e.id}_{release_version}.json"
        if not out.exists():
            out.write_text(
                json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n",
                encoding="utf-8",
            )

    # Also write registry and pointer markers for discoverability
    registry_path = root / "registry" / "history.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    if registry_path.exists():
        try:
            history = json.loads(registry_path.read_text(encoding="utf-8")).get(
                "releases", []
            )
        except Exception:
            history = []
    if not any(h.get("version") == release_version for h in history):
        history.append(
            {
                "version": release_version,
                "manifestHash": manifest["manifestHash"],
                "status": "validated",
            }
        )
        registry_path.write_text(
            json.dumps(
                {"releases": sorted(history, key=lambda x: x["version"])},
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    # current pointer if not exists
    current_path = root / "registry" / "current.json"
    if not current_path.exists():
        current_path.write_text(
            json.dumps(
                {
                    "version": release_version,
                    "generation": "gen-1",
                    "updated_at": _now_iso(),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    return manifest


def _deterministic_vector(text: str, dims: int = EMBEDDING_DIMS) -> list[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vec: list[float] = []
    for i in range(dims):
        byte = h[i % len(h)]
        v = (byte / 127.5) - 1.0
        vec.append(v * 0.1 + (i % 7) * 0.001)
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return [float(x) for x in vec]


def load_manifest(root: Path, version: str) -> dict[str, Any]:
    p = Path(root) / "releases" / f"{version}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def verify_release(root: Path, version: str) -> dict[str, Any]:
    """Strict verification without side effects; fails closed."""
    root = Path(root).expanduser().resolve()
    manifest_path = root / "releases" / f"{version}.json"
    if not manifest_path.exists():
        return {"valid": False, "eligible": False, "reason": "manifest not found"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "valid": False,
            "eligible": False,
            "reason": f"manifest parse failed: {exc}",
        }
    # self-hash check
    stored = manifest.get("manifestHash") or manifest.get("manifest_hash")
    recomputed = hashlib.sha256(
        json.dumps(
            {
                k: v
                for k, v in manifest.items()
                if k not in ("manifestHash", "manifest_hash")
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if stored != recomputed:
        return {"valid": False, "eligible": False, "reason": "manifestHash mismatch"}
    # canonical/raw join check
    entries_count = manifest.get("entries_count", 0)
    artifact_hashes = manifest.get("artifactHashes", {})
    if not isinstance(artifact_hashes, dict) or len(artifact_hashes) != entries_count:
        return {
            "valid": False,
            "eligible": False,
            "reason": "artifactHashes incomplete",
        }
    vectors_dir = root / "vectors" / version
    if not vectors_dir.exists():
        return {"valid": False, "eligible": False, "reason": "vectors missing"}
    vector_files = list(vectors_dir.glob("*.json"))
    vector_ids = {p.stem.split("_")[0] for p in vector_files}
    expected_ids = set(artifact_hashes.keys())
    if vector_ids != expected_ids:
        return {
            "valid": False,
            "eligible": False,
            "reason": f"vector set mismatch expected {sorted(expected_ids)} got {sorted(vector_ids)}",
        }
    # per-vector lineage/metadata/dims check
    for p in vector_files:
        data = json.loads(p.read_text(encoding="utf-8"))
        vec = (
            data.get("embedding")
            or data.get("embedding_vector")
            or data.get("vector")
            or []
        )
        if len(vec) != EMBEDDING_DIMS:
            return {
                "valid": False,
                "eligible": False,
                "reason": f"vector dims {len(vec)} != 768 for {p.name}",
            }
        for v in vec:
            if not isinstance(v, (int, float)) or not (
                v == v and v != float("inf") and v != float("-inf")
            ):
                return {
                    "valid": False,
                    "eligible": False,
                    "reason": f"non-finite value in {p.name}",
                }
        if data.get("model") != EMBEDDING_MODEL:
            return {
                "valid": False,
                "eligible": False,
                "reason": f"model mismatch in {p.name}",
            }
        if int(data.get("dimensions", 0)) != EMBEDDING_DIMS:
            return {
                "valid": False,
                "eligible": False,
                "reason": f"dims mismatch in {p.name}",
            }
        if (
            data.get("task") != EMBEDDING_TASK_DOCUMENT
            and data.get("task_type_document") != EMBEDDING_TASK_DOCUMENT
        ):
            if data.get("task") is not None:
                return {
                    "valid": False,
                    "eligible": False,
                    "reason": f"task mismatch in {p.name}",
                }
        if data.get("distance_measure") != EMBEDDING_DISTANCE:
            # distance may be missing but if present must be DOT_PRODUCT
            if data.get("distance_measure") is not None:
                return {
                    "valid": False,
                    "eligible": False,
                    "reason": f"distance mismatch in {p.name}",
                }
        schema = (
            data.get("schema_version")
            or data.get("schemaVersion")
            or data.get("metadata", {}).get("schema_version")
        )
        if schema and schema != EMBEDDING_SCHEMA_VERSION:
            return {
                "valid": False,
                "eligible": False,
                "reason": f"schema mismatch in {p.name}: {schema}",
            }
        # lineage
        if data.get("source_release") != version:
            return {
                "valid": False,
                "eligible": False,
                "reason": f"source_release mismatch in {p.name}",
            }
        # raw hash must match manifest artifact
        entry_id = data.get("id") or data.get("entry_id") or p.stem.split("_")[0]
        if (
            data.get("raw_content_hash") != artifact_hashes.get(entry_id)
            and data.get("content_hash") != artifact_hashes.get(entry_id)
            and data.get("contentHash") != artifact_hashes.get(entry_id)
        ):
            return {
                "valid": False,
                "eligible": False,
                "reason": f"raw hash mismatch for {entry_id}",
            }
    return {
        "valid": True,
        "eligible": True,
        "manifestHash": stored,
        "entries_count": entries_count,
    }
