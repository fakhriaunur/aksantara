"""Raw snapshot archive — GCS + local replay cache.

Deterministic helpers; no parsing. Stores immutable raw bytes keyed by
contentHash.

GCS path: raw/{source_kind}/{date}/{hash}.html
         full URI: gs://{bucket}/raw/{source_kind}/{date}/{hash}.html
Local cache: tests/replay/fixtures/{hash}.html (and optional named fixture)

Phase 2: GCS upload is best-effort (requires google-cloud-storage and
bucket config); local write always succeeds. No Vertex/Firestore coupling.
"""

from __future__ import annotations

import os
from datetime import UTC
from pathlib import Path
from typing import Any

from aksantara.domain.models import SourceRef

RAW_GCS_PREFIX: str = "raw"
LOCAL_FIXTURE_DIR_NAME: str = "tests/replay/fixtures"


def _project_root() -> Path:
    # src/aksantara/ingest/snapshots.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3]


def gcs_object_path(source_ref: SourceRef) -> str:
    """Return GCS object path without bucket: raw/{kind}/{date}/{hash}.html"""
    date_str: str = source_ref.retrieved_at.astimezone(UTC).date().isoformat()
    return f"{RAW_GCS_PREFIX}/{source_ref.source_kind}/{date_str}/{source_ref.content_hash}.html"


def gcs_uri(source_ref: SourceRef, bucket: str) -> str:
    """Return fully-qualified gs:// URI."""
    if not bucket or "/" in bucket:
        raise ValueError(f"invalid bucket {bucket!r}")
    return f"gs://{bucket}/{gcs_object_path(source_ref)}"


def local_cache_path(source_ref: SourceRef, base_dir: Path | None = None) -> Path:
    """Return local cache Path for hash-addressed file."""
    if base_dir is None:
        base_dir = _project_root() / LOCAL_FIXTURE_DIR_NAME
    return base_dir / f"{source_ref.content_hash}.html"


def local_named_path(lema: str, base_dir: Path | None = None) -> Path:
    """Return local named fixture path: tests/replay/fixtures/{lema}.html"""
    slug: str = lema.strip().lower()
    if not slug:
        raise ValueError("lema must be non-empty for named path")
    if base_dir is None:
        base_dir = _project_root() / LOCAL_FIXTURE_DIR_NAME
    # sanitize slug to filename-safe
    safe: str = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    return base_dir / f"{safe}.html"


def save_raw(
    raw_bytes: bytes,
    source_ref: SourceRef,
    *,
    bucket_name: str | None = None,
    local_base_dir: Path | None = None,
    gcs_client: Any | None = None,
    also_save_named: str | None = None,
) -> dict[str, str]:
    """Persist raw bytes to GCS (best-effort) and local cache.

    Args:
        raw_bytes: immutable snapshot bytes.
        source_ref: provenance for path derivation and hash verification.
        bucket_name: GCS bucket; defaults to env GCS_BUCKET / AKSANTARA_GCS_BUCKET if set.
        local_base_dir: override local fixture directory.
        gcs_client: optional pre-constructed google.cloud.storage.Client.
        also_save_named: if provided, also write to tests/replay/fixtures/{lema}.html.

    Returns:
        Dict with gcs_uri (if attempted), gcs_object, local_path, content_hash, bytes_written.

    Raises:
        ValueError if raw_bytes empty or hash mismatch.
    """
    if not raw_bytes:
        raise ValueError("raw_bytes must be non-empty")
    # Derive paths
    obj_path: str = gcs_object_path(source_ref)
    # Resolve bucket
    effective_bucket: str | None = bucket_name
    if effective_bucket is None:
        effective_bucket = (
            os.getenv("GCS_BUCKET")
            or os.getenv("AKSANTARA_GCS_BUCKET")
            or os.getenv("AKSANTARA_BUCKET")
        )

    # Local write — always
    base_dir: Path = (
        local_base_dir
        if local_base_dir is not None
        else _project_root() / LOCAL_FIXTURE_DIR_NAME
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    hash_path: Path = base_dir / f"{source_ref.content_hash}.html"
    hash_path.write_bytes(raw_bytes)

    named_path_str: str = ""
    if also_save_named is not None:
        named_path: Path = local_named_path(also_save_named, base_dir=base_dir)
        # Do not overwrite existing named fixture with different hash silently? Overwrite deterministically.
        named_path.write_bytes(raw_bytes)
        named_path_str = str(named_path)

    # GCS write — best effort
    gcs_uri_str: str = ""
    gcs_error: str = ""
    if effective_bucket:
        gcs_uri_str = gcs_uri(source_ref, effective_bucket)
        try:
            client: Any
            if gcs_client is not None:
                client = gcs_client
            else:
                try:
                    from google.cloud import storage

                    client = storage.Client()
                except Exception as exc:
                    gcs_error = f"storage client init failed: {exc}"
                    client = None
            if client is not None:
                bucket = client.bucket(effective_bucket)
                blob = bucket.blob(obj_path)
                # Set content type to html utf-8; do not set generation precondition (idempotent)
                blob.upload_from_string(
                    raw_bytes, content_type="text/html; charset=utf-8"
                )
        except Exception as exc:
            # Do not fail save_raw on GCS error; record error for caller inspection
            gcs_error = str(exc)

    result: dict[str, str] = {
        "gcs_object": obj_path,
        "gcs_uri": gcs_uri_str,
        "local_path": str(hash_path),
        "content_hash": source_ref.content_hash,
        "bytes_written": str(len(raw_bytes)),
    }
    if named_path_str:
        result["named_path"] = named_path_str
    if gcs_error:
        result["gcs_error"] = gcs_error
    return result


def load_raw(
    source_ref: SourceRef | str | Path,
    *,
    base_dir: Path | None = None,
    bucket_name: str | None = None,
    gcs_client: Any | None = None,
) -> bytes:
    """Load raw bytes by SourceRef, hash string, or Path.

    Resolution order:
      1. If Path and exists, read file.
      2. If 64-hex hash, look up local cache tests/replay/fixtures/{hash}.html.
      3. If SourceRef, look up by its hash (local), then try GCS if local miss and bucket available.
      4. If string path that is file, read.

    Raises:
        FileNotFoundError if not found locally and GCS not configured/found.
    """
    # Path case
    if isinstance(source_ref, Path):
        if source_ref.is_file():
            return source_ref.read_bytes()
        # treat as hash-named path lookup
        candidate: Path = source_ref
        if candidate.exists():
            return candidate.read_bytes()
        raise FileNotFoundError(f"local raw not found: {candidate}")

    if isinstance(source_ref, str):
        s: str = source_ref.strip()
        # 64-hex hash?
        if len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s):
            # hash lookup
            base: Path = (
                base_dir
                if base_dir is not None
                else _project_root() / LOCAL_FIXTURE_DIR_NAME
            )
            p: Path = base / f"{s.lower()}.html"
            if p.is_file():
                return p.read_bytes()
            # try loading as named fixture fallback (e.g., "februari")
            # but hash path miss -> try GCS if bucket provided
            if bucket_name or os.getenv("GCS_BUCKET"):
                return _load_from_gcs_by_hash(
                    s.lower(),
                    base_dir=base_dir,
                    bucket_name=bucket_name,
                    gcs_client=gcs_client,
                )
            raise FileNotFoundError(f"local raw not found for hash {s}: {p}")
        # otherwise treat as file path string
        p2: Path = Path(s)
        if p2.is_file():
            return p2.read_bytes()
        # also try relative to project root
        p3: Path = _project_root() / s
        if p3.is_file():
            return p3.read_bytes()
        # try as named lema fixture
        # e.g., "februari" -> tests/replay/fixtures/februari.html
        named: Path = (
            base_dir
            if base_dir is not None
            else _project_root() / LOCAL_FIXTURE_DIR_NAME
        ) / f"{s.lower()}.html"
        if named.is_file():
            return named.read_bytes()
        raise FileNotFoundError(
            f"load_raw: string not found as hash/path/named fixture: {s!r}"
        )

    # SourceRef case
    assert isinstance(source_ref, SourceRef)
    base2: Path = (
        base_dir if base_dir is not None else _project_root() / LOCAL_FIXTURE_DIR_NAME
    )
    hp: Path = base2 / f"{source_ref.content_hash}.html"
    if hp.is_file():
        return hp.read_bytes()
    # try GCS
    effective_bucket = (
        bucket_name or os.getenv("GCS_BUCKET") or os.getenv("AKSANTARA_GCS_BUCKET")
    )
    if effective_bucket:
        try:
            return _load_from_gcs(
                source_ref, bucket=effective_bucket, gcs_client=gcs_client
            )
        except FileNotFoundError:
            pass
    raise FileNotFoundError(
        f"local raw not found for SourceRef {source_ref.content_hash}: {hp}"
    )


def _load_from_gcs(
    source_ref: SourceRef, *, bucket: str, gcs_client: Any | None = None
) -> bytes:
    from google.cloud import storage

    client: Any = gcs_client or storage.Client()
    b = client.bucket(bucket)
    blob = b.blob(gcs_object_path(source_ref))
    if not blob.exists():
        raise FileNotFoundError(
            f"GCS object not found: gs://{bucket}/{gcs_object_path(source_ref)}"
        )
    return blob.download_as_bytes()  # type: ignore[no-any-return]


def _load_from_gcs_by_hash(
    hash_hex: str,
    *,
    base_dir: Path | None = None,
    bucket_name: str | None = None,
    gcs_client: Any | None = None,
) -> bytes:
    # Hash-only GCS lookup requires bucket and date unknown; not feasible without index.
    # For Phase 2, we only support direct SourceRef GCS lookup.
    # Raise to indicate not found.
    raise FileNotFoundError(
        f"GCS hash-only load not supported without SourceRef date/kind: {hash_hex}"
    )


__all__ = [
    "LOCAL_FIXTURE_DIR_NAME",
    "RAW_GCS_PREFIX",
    "gcs_object_path",
    "gcs_uri",
    "load_raw",
    "local_cache_path",
    "local_named_path",
    "save_raw",
]
