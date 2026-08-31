"""Ingest package — transport, rate limiting, archiving."""

from aksantara.ingest.checkpoint_catalog import (
    normalize_stable_key,
    selection_keys,
)
from aksantara.ingest.checkpoint_types import (
    CATALOG_SCHEMA_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    CatalogValidationError,
    CheckpointConflictError,
    CheckpointError,
    CheckpointNotFoundError,
    CheckpointPersistenceError,
    CheckpointPreflight,
    LimitValidationError,
    RunResult,
)
from aksantara.ingest.fallback import fetch_fallback
from aksantara.ingest.official import fetch_official
from aksantara.ingest.rate_limit import (
    DEFAULT_RATE_LIMITER,
    FALLBACK_RATE_LIMITER,
    OFFICIAL_RATE_LIMITER,
    RateLimiter,
    TokenBucket,
    calculate_backoff,
)
from aksantara.ingest.snapshots import (
    RAW_SNAPSHOT_SCHEMA_VERSION,
    RawSnapshotStore,
    gcs_object_path,
    gcs_uri,
    load_raw,
    local_cache_path,
    save_raw,
)

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_RATE_LIMITER",
    "FALLBACK_RATE_LIMITER",
    "OFFICIAL_RATE_LIMITER",
    "RAW_SNAPSHOT_SCHEMA_VERSION",
    "CatalogValidationError",
    "CheckpointConflictError",
    "CheckpointDriver",
    "CheckpointError",
    "CheckpointNotFoundError",
    "CheckpointPersistenceError",
    "CheckpointPreflight",
    "LimitValidationError",
    "RateLimiter",
    "RawSnapshotStore",
    "RunResult",
    "TokenBucket",
    "calculate_backoff",
    "fetch_fallback",
    "fetch_official",
    "gcs_object_path",
    "gcs_uri",
    "load_raw",
    "local_cache_path",
    "normalize_stable_key",
    "save_raw",
    "selection_keys",
]


def __getattr__(name: str) -> object:
    """Load checkpoint driver symbols lazily to keep package imports acyclic."""
    if name == "CheckpointDriver":
        from aksantara.ingest.checkpoint import CheckpointDriver

        globals()[name] = CheckpointDriver
        return CheckpointDriver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
