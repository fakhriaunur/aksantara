"""Provenance hash helpers for Aksantara.

All canonical records carry a sha256 contentHash over the immutable raw
snapshot. Re-embedding and replay gates compare against this hash.

Determinism rules:
- Hash is always hex-encoded lower-case sha256.
- Str input is encoded as UTF-8 before hashing.
- Dict/list canonical JSON uses sort_keys=True, separators=(',', ':'),
  ensure_ascii=False to guarantee cross-run stability.

No I/O in this module; pure functions only.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash_bytes(data: bytes) -> str:
    """Return hex sha256 of raw bytes.

    Args:
        data: raw snapshot bytes (e.g., HTML file content).

    Returns:
        64-character lower-case hex digest.
    """
    return hashlib.sha256(data).hexdigest()


def content_hash(text: str) -> str:
    """Return hex sha256 of UTF-8 encoded text.

    Args:
        text: raw snapshot text.

    Returns:
        64-character lower-case hex digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json_hash(payload: dict[str, Any] | list[Any]) -> str:
    """Return hex sha256 of deterministic JSON serialization.

    Keys are sorted, whitespace is stripped, and ensure_ascii is False
    so that Indonesian characters hash identically across platforms.

    Args:
        payload: JSON-serializable dict or list.

    Returns:
        64-character lower-case hex digest.
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return content_hash(canonical)


def verify_content_hash(data: bytes, expected_hex: str) -> bool:
    """Constant-time comparison helper for contentHash verification.

    Args:
        data: raw bytes to hash.
        expected_hex: expected 64-char hex digest.

    Returns:
        True if hash matches (case-insensitive), False otherwise.
    """
    actual = content_hash_bytes(data)
    # Normalize expected to lower-case for comparison.
    return actual == expected_hex.lower()


__all__ = [
    "canonical_json_hash",
    "content_hash",
    "content_hash_bytes",
    "verify_content_hash",
]
