"""Provenance hash helpers for Aksantara.

Raw snapshot hashes and canonical content hashes are deliberately separate
identities.  The former addresses immutable source bytes; the latter
addresses the deterministic lexical serialization used by candidate and
embedding gates.

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

# Fields which constitute lexical canonical content.  Source URL, retrieval
# time, and parser/review metadata are lineage, not lexical content.  Keeping
# this list here gives conflict detection and candidate/embedding planning one
# published serialization rule.
CANONICAL_CONTENT_FIELDS: tuple[str, ...] = (
    "id",
    "lema",
    "sub_lema",
    "ejaan",
    "kelas_kata",
    "makna",
    "contoh",
    "turunan",
    "bentuk_baku",
    "bentuk_tidak_baku",
    "pelafalan",
    "pemenggalan",
    "etimologi",
    "labels",
    "status",
)


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


def canonical_json_hash(payload: Any) -> str:
    """Return hex sha256 of deterministic JSON serialization.

    Keys are sorted, whitespace is stripped, and ensure_ascii is False
    so that Indonesian characters hash identically across platforms.

    Args:
        payload: JSON-serializable value.

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


def canonical_content_payload(entry: Any) -> dict[str, Any]:
    """Return the versioned lexical serialization for an entry.

    ``entry`` may be a :class:`KBBIEntry` or a JSON-compatible mapping.  Only
    fields that define lexical content are retained and field order is fixed
    by ``CANONICAL_CONTENT_FIELDS``.  This intentionally excludes ``source``
    and volatile/review metadata while preserving list order inside lexical
    fields.
    """
    if hasattr(entry, "model_dump"):
        value = entry.model_dump(mode="json")
    elif isinstance(entry, dict):
        value = entry
    else:
        raise TypeError("entry must be a KBBIEntry or mapping")
    return {
        field: value.get(field) for field in CANONICAL_CONTENT_FIELDS if field in value
    }


def canonical_content_hash(entry: Any) -> str:
    """Hash the deterministic lexical serialization of ``entry``."""
    return canonical_json_hash(canonical_content_payload(entry))


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
    "CANONICAL_CONTENT_FIELDS",
    "canonical_content_hash",
    "canonical_content_payload",
    "canonical_json_hash",
    "content_hash",
    "content_hash_bytes",
    "verify_content_hash",
]
