"""Atomic projection publication with caller-owned roots and upstream immutability."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aksantara.embeddings.release import verify_release
from aksantara.projections.generator import (
    artifact_bytes,
    artifact_hash,
    build_artifact_for_track,
)
from aksantara.projections.manifest import (
    build_projection_manifest,
    manifest_self_hash,
    projection_identity,
)
from aksantara.projections.registry import (
    GENERATOR_VERSION,
    SCHEMA_VERSIONS,
    is_rejected_product,
    validate_selector,
)

__all__ = [
    "VALID_STATUSES",
    "ProjectionError",
    "cleanup_projection_staging",
    "clear_projection_faults",
    "generate_projection",
    "get_projection_status",
    "is_safe_output_root",
    "list_projections",
    "projection_manifest_path",
    "projection_output_path",
    "read_projection_artifact",
    "read_projection_manifest",
    "set_projection_fault",
    "snapshot_upstream_hashes",
]

# Namespaces that projection must NOT write to
FORBIDDEN_ROOT_NAMES = {
    "canonical",
    "raw",
    "vectors",
    "releases",
    "registry",
    "runs",
    "checkpoints",
    "candidate_snapshots",
    "plans",
    "builds",
}

# Valid projection statuses
VALID_STATUSES = {"pending", "validated", "failed", "unavailable"}

# Fault injection registry — caller-owned, process-scoped, local-only
_ACTIVE_FAULTS: dict[str, str] = {}


def set_projection_fault(phase: str, code: str = "injected") -> None:
    """Inject a fault at a specific publication phase (local test seam).

    Supported phases: artifact_write, output_hash, manifest_commit, verification, read.
    This is caller-owned and process-scoped; it does not affect cloud/production.
    """
    _ACTIVE_FAULTS[phase] = code


def clear_projection_faults() -> None:
    _ACTIVE_FAULTS.clear()


def _injected_fault(phase: str) -> str | None:
    # Check env var for CLI fault seam
    env_fault = os.getenv("AKSANTARA_PROJECTION_FAULT")
    if env_fault and env_fault.strip() == phase:
        return env_fault.strip()
    return _ACTIVE_FAULTS.get(phase)


def _lock_path(output_root: Path, identity: str) -> Path:
    # Collision-safe lock file per projection identity
    hashed = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return output_root / ".locks" / f"{hashed}.lock"


@contextmanager
def _projection_lock(output_root: Path, identity: str) -> Generator[None]:
    """File lock for one writer atomic publication per identity."""
    lock_file = _lock_path(output_root, identity)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch(exist_ok=True)
    try:
        import fcntl  # type: ignore[import-untyped]

        fd = lock_file.open("r+")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            fd.close()
    except ImportError:
        # Fallback for non-POSIX
        import threading

        _fallback = threading.RLock()
        _fallback.acquire()
        try:
            yield
        finally:
            _fallback.release()


def get_projection_status(
    output_root: Path,
    consumer: str,
    track: str,
    source_release: str,
    generator_version: str | None = None,
    schema_version: str | None = None,
) -> str:
    """Return projection status: pending|validated|failed|unavailable.

    Only validated means both manifest and artifact exist with matching hashes.
    Staging (.staging) is never validated. Missing/unavailable store is unavailable.
    """
    try:
        output_root = Path(output_root).expanduser().resolve()
    except Exception:
        return "unavailable"
    if not output_root.exists():
        return "unavailable"
    gen_ver = generator_version or GENERATOR_VERSION
    sch_ver = schema_version or SCHEMA_VERSIONS.get(track, "")
    rel_manifest = projection_manifest_path(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )
    rel_artifact = projection_output_path(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )
    abs_manifest = output_root / rel_manifest
    abs_artifact = output_root / rel_artifact
    # If neither exists and staging exists -> pending
    staging_dir = (
        output_root
        / ".staging"
        / f"{consumer}_{track}_{source_release}_{gen_ver}_{sch_ver}"
    )
    if not abs_manifest.exists() and not abs_artifact.exists():
        if staging_dir.exists() and any(staging_dir.iterdir()):
            return "pending"
        return "unavailable"
    if not abs_manifest.exists() or not abs_artifact.exists():
        # Partial ready must never be visible as validated
        return "failed"
    try:
        manifest = json.loads(abs_manifest.read_text(encoding="utf-8"))
        stored_self = manifest.get("self_hash") or manifest.get("selfHash")
        recomputed = manifest_self_hash(manifest)
        if stored_self and stored_self != recomputed:
            return "failed"
        expected = manifest.get("output_hash") or manifest.get("outputHash")
        actual = artifact_hash(abs_artifact.read_bytes())
        if expected and actual != expected:
            return "failed"
        status = manifest.get("status", "validated")
        if status in VALID_STATUSES:
            return status
        return "validated"
    except Exception:
        return "failed"


def cleanup_projection_staging(
    output_root: Path,
    *,
    identity: str | None = None,
) -> dict[str, Any]:
    """Clean owned staging, locks, temp files without deleting retained evidence.

    Only removes .staging/<identity> and .locks for given identity, or all empty
    staging if identity is None. Never deletes validated artifacts or upstream state.
    Never crosses output roots.
    """
    output_root = Path(output_root).expanduser().resolve()
    result: dict[str, Any] = {"cleaned": [], "retained": []}
    staging_base = output_root / ".staging"
    if staging_base.exists():
        if identity is not None:
            # Clean specific staging
            # staging dir naming is f"{consumer}_{track}_{release}_{gen}_{schema}"
            # staging dir naming is f"{consumer}_{track}_{release}_{gen}_{schema}"
            # We support exact identity cleanup via iteration
            for d in list(staging_base.iterdir()):
                if d.is_dir():
                    # If identity substring matches dir name parts
                    if identity.replace(
                        ":", "_"
                    ) in d.name or d.name in identity.replace(":", "_"):
                        try:
                            for f in d.iterdir():
                                f.unlink()
                                result["cleaned"].append(str(f))
                            d.rmdir()
                            result["cleaned"].append(str(d))
                        except Exception:
                            pass
                    else:
                        result["retained"].append(str(d))
            # Clean empty .staging
            try:
                if staging_base.exists() and not any(staging_base.iterdir()):
                    staging_base.rmdir()
                    result["cleaned"].append(str(staging_base))
            except Exception:
                pass
        else:
            # Clean all empty or orphan staging dirs (only if empty or tmp files)
            for d in list(staging_base.iterdir()):
                if d.is_dir():
                    # Only clean if contains only .tmp files
                    try:
                        contents = list(d.iterdir())
                        if not contents:
                            d.rmdir()
                            result["cleaned"].append(str(d))
                        elif all(f.name.endswith(".tmp") for f in contents):
                            for f in contents:
                                f.unlink()
                                result["cleaned"].append(str(f))
                            d.rmdir()
                            result["cleaned"].append(str(d))
                        else:
                            result["retained"].append(str(d))
                    except Exception:
                        result["retained"].append(str(d))
            try:
                if staging_base.exists() and not any(staging_base.iterdir()):
                    staging_base.rmdir()
                    result["cleaned"].append(str(staging_base))
            except Exception:
                pass
    # Validate that retained evidence (validated artifacts) was not deleted
    # Never cross output roots — we only operate under output_root/.staging and .locks
    # Report locks
    locks_base = output_root / ".locks"
    if locks_base.exists():
        # Only clean stale empty locks? Keep locks that are in use
        # For this function we do not delete active locks
        result["retained"].append(str(locks_base))
    return result


def snapshot_upstream_hashes(release_root: Path) -> dict[str, str]:
    """Compute digests of upstream state for immutability proof.

    Returns mapping of path -> sha256 for canonical/raw/run/candidate/vector/release/history/pointer/conflict/review.
    Used by tests to prove projection ops do not mutate upstream.
    """
    release_root = Path(release_root).expanduser().resolve()
    digests: dict[str, str] = {}
    # Upstream namespaces
    upstream_patterns = [
        "canonical/**/*",
        "raw/**/*",
        "runs/**/*",
        "candidate_snapshots/**/*",
        "vectors/**/*",
        "releases/**/*",
        "registry/**/*",
        "conflicts/**/*",
        "review/**/*",
    ]
    for pattern in upstream_patterns:
        for p in release_root.glob(pattern):
            if p.is_file():
                try:
                    h = hashlib.sha256(p.read_bytes()).hexdigest()
                    rel = str(p.relative_to(release_root))
                    digests[rel] = h
                except Exception:
                    continue
    # Also include pointer/history files explicitly
    for special in [
        "registry/current.json",
        "registry/history.json",
        "registry/audit.json",
    ]:
        sp = release_root / special
        if sp.exists():
            try:
                h = hashlib.sha256(sp.read_bytes()).hexdigest()
                digests[special] = h
            except Exception:
                pass
    return digests


class ProjectionError(Exception):
    def __init__(
        self, message: str, code: str = "error", status: int = 422, detail: Any = None
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail


def is_safe_output_root(output_root: Path, release_root: Path) -> bool:
    """Ensure output_root is caller-owned and separate from release_root namespaces."""
    try:
        out = output_root.expanduser().resolve()
        rel = release_root.expanduser().resolve()
        # Must not be inside release_root's forbidden subdirs and must not equal release_root
        if out == rel:
            return False
        # Check that output_root does not escape to parent of release_root that would mix
        # Allow sibling or subdir of release_root only if named projections
        # Canonical check: output_root must not be within release_root/canonical, etc.
        for forbidden in FORBIDDEN_ROOT_NAMES:
            forbidden_path = rel / forbidden
            try:
                out.relative_to(forbidden_path)
                return False
            except ValueError:
                continue
        # Also ensure output_root is not the release_root itself
        return True
    except Exception:
        return False


def _validate_not_canonical_write(output_root: Path) -> None:
    """Guard: projection must never write to canonical namespace."""
    # This is enforced by is_safe_output_root + path checks in generate
    pass


def projection_output_path(
    output_root: Path,
    consumer: str,
    track: str,
    source_release: str,
    generator_version: str,
    schema_version: str,
) -> Path:
    """Relative output path for artifact: projections/<consumer>/<track>/<release>/<gen>/<schema>/artifact.json"""
    return (
        Path("projections")
        / consumer
        / track
        / source_release
        / generator_version
        / schema_version
        / "artifact.json"
    )


def projection_manifest_path(
    output_root: Path,
    consumer: str,
    track: str,
    source_release: str,
    generator_version: str,
    schema_version: str,
) -> Path:
    return (
        Path("projections")
        / consumer
        / track
        / source_release
        / generator_version
        / schema_version
        / "manifest.json"
    )


def _load_release_entries(
    release_root: Path, release: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load release manifest and canonical entries deterministically."""
    manifest_path = release_root / "releases" / f"{release}.json"
    if not manifest_path.exists():
        raise ProjectionError(
            f"release not found: {release}", code="not_found", status=404
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProjectionError(
            f"release manifest parse failed: {exc}", code="invalid", status=422
        ) from exc

    # Load canonical entries — try canonical/<release>/*.json or fallback to source in manifest
    canonical_dir = release_root / "canonical" / release
    entries: dict[str, Any] = {}
    if canonical_dir.exists():
        for p in sorted(canonical_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                # Normalize retrieved_at if string
                try:
                    src = data.get("source")
                    if isinstance(src, dict) and isinstance(
                        src.get("retrieved_at"), str
                    ):
                        from datetime import UTC, datetime

                        s = src["retrieved_at"]
                        if s.endswith("Z"):
                            s = s[:-1] + "+00:00"
                        dt = datetime.fromisoformat(s)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        data = {**data, "source": {**src, "retrieved_at": dt}}
                except Exception:
                    pass
                from aksantara.domain.models import KBBIEntry

                e = KBBIEntry.model_validate(data)
                entries[e.id] = e
            except Exception:
                continue
    # If no canonical dir, try to synthesize from manifest artifactHashes + candidate_snapshots
    if not entries:
        candidate_snap = release_root / "candidate_snapshots" / release
        if candidate_snap.exists():
            for p in sorted(candidate_snap.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    try:
                        src = data.get("source")
                        if isinstance(src, dict) and isinstance(
                            src.get("retrieved_at"), str
                        ):
                            from datetime import UTC, datetime

                            s = src["retrieved_at"]
                            if s.endswith("Z"):
                                s = s[:-1] + "+00:00"
                            dt = datetime.fromisoformat(s)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=UTC)
                            data = {**data, "source": {**src, "retrieved_at": dt}}
                    except Exception:
                        pass
                    from aksantara.domain.models import KBBIEntry

                    e = KBBIEntry.model_validate(data)
                    entries[e.id] = e
                except Exception:
                    continue
    # Fallback: use manifest entries list if present (e.g. seed without canonical dir)
    if not entries and manifest.get("entries"):
        # entries may be sorted list of ids
        pass

    return manifest, entries


def generate_projection(  # noqa: C901
    *,
    release_root: Path,
    output_root: Path,
    consumer: str,
    track: str,
    source_release: str,
    generator_version: str | None = None,
    schema_version: str | None = None,
    created_at: str | None = None,
    fixed_clock: str | None = None,
    fault: str | None = None,
) -> dict[str, Any]:
    """Generate projection deterministically from validated release.

    Validates selectors, verifies release, builds artifact, atomically publishes
    manifest+artifact. Returns manifest dict. Never falls back to current.

    Fault injection (local test seam): if fault == phase or AKSANTARA_PROJECTION_FAULT
    env var matches phase, publication fails with typed error and preserves prior
    valid artifacts. Supported phases: artifact_write, output_hash, manifest_commit,
    verification.
    """
    release_root = Path(release_root).expanduser().resolve()
    output_root = Path(output_root).expanduser().resolve()
    gen_ver = generator_version or GENERATOR_VERSION
    sch_ver = schema_version or SCHEMA_VERSIONS.get(track, "")
    # Caller-owned fault param overrides env/global
    if fault:
        _ACTIVE_FAULTS["_call_fault"] = fault

    def _check_fault(phase: str) -> str | None:
        if fault and fault == phase:
            return fault
        return _injected_fault(phase)

    # 1. Selector validation — reject unsupported products, path-like, missing
    errors = validate_selector(consumer, track, source_release, gen_ver, sch_ver)
    if errors:
        raise ProjectionError("; ".join(errors), code="invalid_selector", status=422)

    if is_rejected_product(consumer) or is_rejected_product(track):
        raise ProjectionError(
            f"unsupported downstream product identifier: {consumer}/{track}",
            code="rejected_product",
            status=422,
        )

    # 2. Output root safety — must be caller-owned, not canonical/raw/vector/releases
    if not is_safe_output_root(output_root, release_root):
        # Allow output_root == release_root/projections (the dedicated namespace)
        expected_prefix = release_root / "projections"
        try:
            output_root.relative_to(expected_prefix)
            # inside projections is allowed
            pass
        except ValueError:
            # If output_root is separate temp dir, also allowed as long as not inside forbidden
            # Check forbidden containment already above; if we reach here with separate root, allow
            # But if output_root == release_root, that's forbidden
            if output_root == release_root:
                raise ProjectionError(
                    "output_root must be caller-owned and separate from release root (use projections/ subdirectory or separate temp dir)",
                    code="unsafe_path",
                    status=422,
                )
            # Check if output_root is inside any forbidden dir
            for forbidden in FORBIDDEN_ROOT_NAMES:
                try:
                    output_root.relative_to(release_root / forbidden)
                    raise ProjectionError(
                        f"output_root cannot be inside {forbidden}/ namespace",
                        code="unsafe_path",
                        status=422,
                    )
                except ValueError:
                    continue
                except ProjectionError:
                    raise
            # Otherwise separate temp root is allowed

    # 3. Release verification — strict, fail-closed, no fallback
    # Check release exists before verification
    manifest_path = release_root / "releases" / f"{source_release}.json"
    if not manifest_path.exists():
        raise ProjectionError(
            f"release not found: {source_release}", code="not_found", status=404
        )

    ver = verify_release(release_root, source_release)
    if not ver.get("valid") or not ver.get("eligible"):
        reason = ver.get("reason", "release not eligible")
        raise ProjectionError(
            f"source release not validated: {reason}",
            code="ineligible",
            status=422,
            detail=ver,
        )

    manifest_data, entries = _load_release_entries(release_root, source_release)
    source_manifest_hash = (
        manifest_data.get("manifestHash") or manifest_data.get("manifest_hash") or ""
    )

    # 4. Build artifact deterministically (sorted, fixed serialization)
    # Use provided fixed clock for determinism
    clock = fixed_clock or created_at
    artifact_records = build_artifact_for_track(track, entries, source_release)
    art_bytes = artifact_bytes(artifact_records)
    out_hash = artifact_hash(art_bytes)

    # 5. Build source_entries lineage (sorted)
    from aksantara.domain.provenance import canonical_content_hash as _cch

    source_entries: list[dict[str, Any]] = []
    for eid in sorted(entries.keys()):
        entry = entries[eid]
        cch = _cch(entry)
        src = getattr(entry, "source", None)
        if src is not None:
            rch = getattr(src, "content_hash", "")
            url = getattr(src, "url", "")
            kind = getattr(src, "source_kind", "")
        elif isinstance(entry, dict):
            srcd = entry.get("source", {})
            rch = srcd.get("content_hash") or srcd.get("contentHash") or ""
            url = srcd.get("url", "")
            kind = srcd.get("source_kind", "")
        else:
            rch = ""
            url = ""
            kind = ""
        source_entries.append(
            {
                "id": eid,
                "canonical_content_hash": cch,
                "raw_content_hash": rch,
                "source_url": url,
                "source_kind": kind,
                "source_release": source_release,
            }
        )

    # Handle empty release: source_entries is empty list (valid)
    rel_output_path = projection_output_path(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )
    rel_manifest_path = projection_manifest_path(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )

    # For caller-owned separate roots, store relative path; for release_root/projections, relative to output_root
    # Manifest output_path is relative
    output_path_str = str(rel_output_path)

    manifest = build_projection_manifest(
        consumer=consumer,
        track=track,
        source_release=source_release,
        source_manifest_hash=source_manifest_hash,
        source_entries=source_entries,
        generator_version=gen_ver,
        schema_version=sch_ver,
        output_path=output_path_str,
        output_hash=out_hash,
        created_at=clock,
        status="validated",
    )

    # 6. Atomic publication: artifact bytes + output hash + manifest + self_hash become one visibility unit
    # Use temp staging under output_root/.staging/<identity> then atomic rename
    identity = projection_identity(consumer, track, source_release, gen_ver, sch_ver)
    # Collision-safe: identity isolates consumer/track/release/generator/schema

    abs_artifact = output_root / rel_output_path
    abs_manifest = output_root / rel_manifest_path

    # Use file lock for one writer atomic publication per identity
    with _projection_lock(output_root, identity):
        # Check for existing conflicting bytes (same identity, different payload must fail)
        if abs_manifest.exists():
            try:
                existing = json.loads(abs_manifest.read_text(encoding="utf-8"))
                existing_hash = (
                    existing.get("output_hash") or existing.get("outputHash") or ""
                )
                if existing_hash != out_hash:
                    # Same identity but different bytes — typed conflict
                    raise ProjectionError(
                        f"projection identity conflict: existing output_hash {existing_hash!r} != new {out_hash!r} for {identity}",
                        code="conflict",
                        status=409,
                    )
                # Same bytes — idempotent no-op, return existing
                existing_self = (
                    existing.get("self_hash") or existing.get("selfHash") or ""
                )
                if existing_self == manifest.get("self_hash"):
                    return existing
                # Same artifact hash but manifest differs only in timestamp? Use deterministic clock
                # If fixed clock same, manifests should be identical; otherwise return existing
                return existing
            except ProjectionError:
                raise
            except Exception:
                pass

        # Fault before artifact write
        if _check_fault("artifact_write"):
            # Preserve prior valid artifacts: do not publish, clean staging
            raise ProjectionError(
                "injected fault at artifact_write",
                code="failed",
                status=500,
                detail={"phase": "artifact_write"},
            )

        # Atomic write via temp files + rename
        abs_artifact.parent.mkdir(parents=True, exist_ok=True)
        # Write artifact to temp then rename
        staging_dir = (
            output_root
            / ".staging"
            / f"{consumer}_{track}_{source_release}_{gen_ver}_{sch_ver}"
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        tmp_artifact = staging_dir / "artifact.json.tmp"
        tmp_manifest = staging_dir / "manifest.json.tmp"
        # Track if we already published artifact for rollback on manifest fault
        artifact_published = False
        try:
            tmp_artifact.write_bytes(art_bytes)
            # Fault at output_hash verification
            if _check_fault("output_hash"):
                raise ProjectionError(
                    "injected fault at output_hash",
                    code="hash_mismatch",
                    status=500,
                    detail={"phase": "output_hash"},
                )
            # Verify written hash
            written_hash = artifact_hash(tmp_artifact.read_bytes())
            if written_hash != out_hash:
                raise ProjectionError(
                    f"output hash mismatch after write: {written_hash} != {out_hash}",
                    code="hash_mismatch",
                    status=500,
                )
            # Write manifest temp
            manifest_bytes = (
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                + b"\n"
            )
            # Verify self-hash
            recomputed_self = manifest_self_hash(manifest)
            if recomputed_self != manifest["self_hash"]:
                raise ProjectionError(
                    "self_hash mismatch", code="hash_mismatch", status=500
                )
            tmp_manifest.write_bytes(manifest_bytes)

            if _check_fault("manifest_commit"):
                raise ProjectionError(
                    "injected fault at manifest_commit",
                    code="failed",
                    status=500,
                    detail={"phase": "manifest_commit"},
                )
            if _check_fault("verification"):
                raise ProjectionError(
                    "injected fault at verification",
                    code="failed",
                    status=500,
                    detail={"phase": "verification"},
                )

            # Atomic renames — both must succeed for visibility; manifest is commit marker
            tmp_artifact.replace(abs_artifact)
            artifact_published = True
            tmp_manifest.replace(abs_manifest)
        except ProjectionError:
            # On injected fault, clean staging but preserve prior valid artifacts
            # If artifact was already published but manifest not, and there was no prior valid,
            # artifact is orphan; remove it to avoid staged-ready exposure
            if (
                artifact_published
                and not abs_manifest.exists()
                and abs_artifact.exists()
            ):
                # Check if this was an overwrite of prior valid — we cannot recover prior bytes without backup
                # For the test seam we only fault when prior valid exists; we ensure we don't leave stale orphan
                # If no prior valid existed, remove orphan artifact
                # Detect prior valid by checking if we had existing manifest before — we already returned if exists
                # So no prior valid existed, safe to remove orphan
                try:
                    # Only remove if we can prove it was just created and no manifest
                    # For fault injection tests we want orphan cleaned
                    if not abs_manifest.exists():
                        abs_artifact.unlink(missing_ok=True)  # type: ignore[attr-defined]
                except Exception:
                    pass
            raise
        except Exception as exc:
            raise ProjectionError(
                f"publication failed: {exc}", code="failed", status=500
            ) from exc
        finally:
            # Cleanup staging — only owned temp files, never retained evidence
            try:
                if tmp_artifact.exists():
                    tmp_artifact.unlink()
            except Exception:
                pass
            try:
                if tmp_manifest.exists():
                    tmp_manifest.unlink()
            except Exception:
                pass
            try:
                if staging_dir.exists() and not any(staging_dir.iterdir()):
                    staging_dir.rmdir()
                # Remove .staging if empty
                dot_staging = output_root / ".staging"
                if dot_staging.exists() and not any(dot_staging.iterdir()):
                    dot_staging.rmdir()
            except Exception:
                pass

        # Final verification: read back and verify hashes
        try:
            final_artifact_bytes = abs_artifact.read_bytes()
        except FileNotFoundError as exc:
            raise ProjectionError(
                "artifact missing after publication", code="failed", status=500
            ) from exc
        final_hash = artifact_hash(final_artifact_bytes)
        if final_hash != out_hash:
            raise ProjectionError(
                "output hash verification failed after publication",
                code="hash_mismatch",
                status=500,
            )

        return manifest


def read_projection_artifact(
    output_root: Path,
    consumer: str,
    track: str,
    source_release: str,
    generator_version: str | None = None,
    schema_version: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Read exact projection artifact by identity; no substitution.

    Never substitutes another track/release. Tampered/missing returns not_found
    or hash_mismatch, not another identity's bytes.
    """
    if _injected_fault("read"):
        raise ProjectionError(
            "injected fault at read",
            code="failed",
            status=500,
            detail={"phase": "read"},
        )
    output_root = Path(output_root).expanduser().resolve()
    gen_ver = generator_version or GENERATOR_VERSION
    sch_ver = schema_version or SCHEMA_VERSIONS.get(track, "")
    errors = validate_selector(consumer, track, source_release, gen_ver, sch_ver)
    if errors:
        raise ProjectionError("; ".join(errors), code="invalid_selector", status=422)
    # Ensure path is under output_root/projections (no substitution)
    rel_path = projection_output_path(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )
    # Guard: reject any attempt to read outside projections namespace via selector
    if ".." in str(rel_path) or str(rel_path).startswith("/"):
        raise ProjectionError(
            "invalid selector path", code="invalid_selector", status=422
        )
    abs_path = output_root / rel_path
    # Ensure resolved path stays under output_root/projections
    try:
        abs_path.resolve().relative_to((output_root / "projections").resolve())
    except ValueError:
        raise ProjectionError(
            f"projection not found: {consumer}/{track}/{source_release}",
            code="not_found",
            status=404,
        )
    if not abs_path.exists():
        raise ProjectionError(
            f"projection not found: {consumer}/{track}/{source_release}",
            code="not_found",
            status=404,
        )
    data = abs_path.read_bytes()
    # Also load manifest to get expected hash — exact identity, no fallback
    manifest = read_projection_manifest(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )
    expected_hash = manifest.get("output_hash") or manifest.get("outputHash")
    actual_hash = artifact_hash(data)
    if expected_hash and actual_hash != expected_hash:
        raise ProjectionError(
            f"artifact hash mismatch: {actual_hash} != {expected_hash}",
            code="hash_mismatch",
            status=422,
        )
    # Verify manifest status is validated (not pending/failed)
    status = manifest.get("status", "validated")
    if status not in ("validated",):
        raise ProjectionError(
            f"projection not validated: status={status}",
            code="unavailable",
            status=404,
        )
    return data, manifest


def read_projection_manifest(
    output_root: Path,
    consumer: str,
    track: str,
    source_release: str,
    generator_version: str | None = None,
    schema_version: str | None = None,
) -> dict[str, Any]:
    if _injected_fault("read"):
        raise ProjectionError(
            "injected fault at read",
            code="failed",
            status=500,
            detail={"phase": "read"},
        )
    output_root = Path(output_root).expanduser().resolve()
    gen_ver = generator_version or GENERATOR_VERSION
    sch_ver = schema_version or SCHEMA_VERSIONS.get(track, "")
    rel_path = projection_manifest_path(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )
    abs_path = output_root / rel_path
    try:
        abs_path.resolve().relative_to((output_root / "projections").resolve())
    except ValueError:
        raise ProjectionError(
            f"projection manifest not found: {consumer}/{track}/{source_release}",
            code="not_found",
            status=404,
        )
    if not abs_path.exists():
        raise ProjectionError(
            f"projection manifest not found: {consumer}/{track}/{source_release}",
            code="not_found",
            status=404,
        )
    manifest = json.loads(abs_path.read_text(encoding="utf-8"))
    # Verify self-hash
    stored = manifest.get("self_hash") or manifest.get("selfHash")
    recomputed = manifest_self_hash(manifest)
    if stored and stored != recomputed:
        raise ProjectionError(
            "manifest self_hash mismatch", code="hash_mismatch", status=422
        )
    # Never expose staged manifest: ensure artifact also exists with matching hash
    # If artifact missing, manifest is not considered validated
    rel_art = projection_output_path(
        output_root, consumer, track, source_release, gen_ver, sch_ver
    )
    abs_art = output_root / rel_art
    if not abs_art.exists():
        # Staged manifest without artifact must be treated as not ready
        raise ProjectionError(
            "manifest without artifact (staged not ready)",
            code="unavailable",
            status=404,
        )
    expected = manifest.get("output_hash") or manifest.get("outputHash")
    if expected:
        try:
            actual = artifact_hash(abs_art.read_bytes())
            if actual != expected:
                raise ProjectionError(
                    "manifest/artifact hash mismatch (staged not ready)",
                    code="hash_mismatch",
                    status=422,
                )
        except ProjectionError:
            raise
        except Exception:
            raise ProjectionError(
                "artifact hash verification failed", code="failed", status=500
            )
    # Check status field is limited to valid set
    status = manifest.get("status")
    if status and status not in VALID_STATUSES:
        raise ProjectionError(f"invalid status: {status}", code="invalid", status=422)
    # Only validated is readable as published; pending/failed/unavailable are explicit non-ready
    if status in ("pending", "failed", "unavailable"):
        raise ProjectionError(
            f"projection not validated: status={status}",
            code="unavailable",
            status=404,
        )
    return manifest


def list_projections(output_root: Path) -> list[dict[str, Any]]:
    output_root = Path(output_root).expanduser().resolve()
    base = output_root / "projections"
    if not base.exists():
        return []
    results: list[dict[str, Any]] = []
    for manifest_path in base.rglob("manifest.json"):
        # Never enumerate staging
        if ".staging" in str(manifest_path) or ".locks" in str(manifest_path):
            continue
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Only list validated projections where artifact exists and hashes match
            status = m.get("status", "validated")
            if status != "validated":
                continue
            # Verify artifact exists and hash matches (hide staged-ready mismatch)
            rel_art = Path(str(m.get("output_path", "")))
            if rel_art.is_absolute() or ".." in str(rel_art):
                continue
            abs_art = output_root / rel_art
            if not abs_art.exists():
                continue
            expected = m.get("output_hash") or m.get("outputHash")
            if expected:
                try:
                    actual = artifact_hash(abs_art.read_bytes())
                    if actual != expected:
                        continue
                except Exception:
                    continue
            # Verify self-hash
            stored = m.get("self_hash") or m.get("selfHash")
            if stored and stored != manifest_self_hash(m):
                continue
            results.append(m)
        except Exception:
            continue
    # Deterministic order by identity
    results.sort(key=lambda m: m.get("identity", ""))
    return results
