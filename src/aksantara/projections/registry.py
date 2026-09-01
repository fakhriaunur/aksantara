"""Projection track/schema registry — generic word/relations only.

Publishes the allowed (consumer, track) pairs, schema versions,
generator versions, serialization rules, and rejected product identifiers.
No separate Hunspell, cspell, Babel, Polyglossia, or Rabu Baku products
are implemented; those identifiers are explicitly rejected.
"""

from __future__ import annotations

__all__ = [
    "ALLOWED_CONSUMERS",
    "ALLOWED_TRACKS",
    "EMPTY_RELEASE_POLICY",
    "GENERATOR_VERSION",
    "OUTPUT_CONTENT_TYPES",
    "REJECTED_PRODUCT_IDENTIFIERS",
    "RELATION_RULE",
    "SCHEMA_VERSIONS",
    "SERIALIZATION_RULES",
    "SOURCE_ENTRIES_RULE",
    "TRACK_SCHEMA_MAP",
    "get_schema_version",
    "is_allowed_consumer",
    "is_allowed_track",
    "is_rejected_product",
    "registry_snapshot",
    "validate_selector",
]

# Allowed generic projection tracks
ALLOWED_TRACKS: tuple[str, ...] = ("word", "relations")
ALLOWED_CONSUMERS: tuple[str, ...] = ("aksantara", "generic")

# Product-specific identifiers that are NOT generic support — must be rejected
REJECTED_PRODUCT_IDENTIFIERS: tuple[str, ...] = (
    "hunspell",
    "cspell",
    "babel",
    "polyglossia",
    "rabu-baku",
    "rabu_baku",
    "rabubaku",
    "hunspell-aff",
    "hunspell-dic",
    "cspell-dict",
)

GENERATOR_VERSION: str = "proj-gen-v1"

SCHEMA_VERSIONS: dict[str, str] = {
    "word": "word-v1",
    "relations": "relations-v1",
}

TRACK_SCHEMA_MAP: dict[str, str] = {
    "word": "word-v1",
    "relations": "relations-v1",
}

SERIALIZATION_RULES: dict[str, str] = {
    "encoding": "UTF-8",
    "json_keys": "sorted (sort_keys=True)",
    "json_separators": "compact (',', ':')",
    "json_ensure_ascii": "False (preserve Unicode)",
    "array_order": "sorted by id for words; sorted by (from,to,type) for relations",
    "number_format": "JSON numbers, finite only",
    "final_newline": "exactly one trailing newline (\\n) for published records",
    "hash_algorithm": "lower-case hex SHA-256 of UTF-8 bytes",
    "determinism": "fixed inputs and clock produce byte-identical artifacts regardless of input file order",
}

OUTPUT_CONTENT_TYPES: dict[str, str] = {
    "word": "application/json",
    "relations": "application/json",
}

EMPTY_RELEASE_POLICY: dict[str, str] = {
    "empty_release": "projection of empty release produces empty word list and empty relations list with valid manifest; not an error",
    "no_entries": "empty artifact is valid JSON with zero entries, deterministic hash, and validated status",
}

SOURCE_ENTRIES_RULE: dict[str, str] = {
    "rule": "all eligible release records — every entry in the validated release manifest is included as a source entry",
    "universe": "all eligible release records (validated, official, active entries in the release manifest)",
    "witness": "each word/relation carries source entry id, canonical hash, raw hash, and source reference",
    "empty": "empty release produces empty source_entries list",
}

RELATION_RULE: dict[str, str] = {
    "direction": "explicit directed edge from nonstandard form to standard form",
    "from_field": "bentuk_tidak_baku element (nonstandard variant string)",
    "to_field": "entry lema (standard form) or bentuk_baku target when entry itself is variant",
    "type": "nonstandard_variant",
    "source_entry": "id of the canonical entry that declares the relation",
    "canonical_field": "bentuk_tidak_baku or bentuk_baku",
    "source_hash": "canonical_content_hash of the source entry",
    "endpoint_lookup": "to endpoint is resolved within the same release; unresolved targets produce relation with to field as declared string but marked unresolved=false still emitted (no invented endpoint)",
    "unresolved_target": "relation is emitted with declared to value; no synthetic entry is created; consumer must handle missing endpoint",
    "normalization": "forms are used as-is from canonical entry (no case folding or trimming beyond source)",
    "duplicate_key": "duplicate (from,to,type) tuples are deduplicated to one relation, sorted deterministically",
    "bentuk_tidak_baku_vs_bentuk_baku": "bentuk_tidak_baku is a list of nonstandard variants pointing to this entry's lema; bentuk_baku is a string standard form when this entry is itself a variant — they cannot imply direction alone, each produces explicit directed edges",
}


def is_allowed_track(track: str) -> bool:
    return track in ALLOWED_TRACKS


def is_allowed_consumer(consumer: str) -> bool:
    return consumer in ALLOWED_CONSUMERS


def is_rejected_product(identifier: str) -> bool:
    return identifier.lower() in REJECTED_PRODUCT_IDENTIFIERS


def get_schema_version(track: str) -> str | None:
    return SCHEMA_VERSIONS.get(track)


def validate_selector(
    consumer: str,
    track: str,
    release: str,
    generator_version: str | None = None,
    schema_version: str | None = None,
) -> list[str]:
    """Validate projection selectors; return list of error messages (empty if valid)."""
    errors: list[str] = []
    if not consumer or not consumer.strip():
        errors.append("consumer is required")
    elif is_rejected_product(consumer):
        errors.append(f"unsupported downstream product identifier: {consumer}")
    elif not is_allowed_consumer(consumer):
        errors.append(
            f"unsupported consumer: {consumer}; allowed: {', '.join(ALLOWED_CONSUMERS)}"
        )
    if not track or not track.strip():
        errors.append("track is required")
    elif is_rejected_product(track):
        errors.append(f"unsupported downstream product identifier: {track}")
    elif not is_allowed_track(track):
        errors.append(
            f"unsupported track: {track}; allowed: {', '.join(ALLOWED_TRACKS)}"
        )
    if not release or not release.strip():
        errors.append("release is required")
    elif "/" in release or "\\" in release or ".." in release:
        errors.append(f"release contains path-like characters: {release}")
    elif not release.replace(".", "").replace("-", "").replace("_", "").isalnum():
        # allow version-like strings but reject path traversal
        if any(c in release for c in ("/", "\\", ":", " ")):
            errors.append(f"release contains invalid characters: {release}")
    if generator_version is not None and generator_version != GENERATOR_VERSION:
        errors.append(
            f"unsupported generator_version: {generator_version}; expected {GENERATOR_VERSION}"
        )
    if schema_version is not None and track in SCHEMA_VERSIONS:
        expected = SCHEMA_VERSIONS[track]
        if schema_version != expected:
            errors.append(
                f"unsupported schema_version for track {track}: {schema_version}; expected {expected}"
            )
    # Path-like checks for consumer/track
    for name, val in (("consumer", consumer), ("track", track)):
        if "/" in val or "\\" in val or ".." in val:
            errors.append(f"{name} contains path-like characters: {val}")
    return errors


def registry_snapshot() -> dict[str, object]:
    """Return full registry for help/discovery surfaces."""
    return {
        "allowed_consumers": list(ALLOWED_CONSUMERS),
        "allowed_tracks": list(ALLOWED_TRACKS),
        "rejected_product_identifiers": list(REJECTED_PRODUCT_IDENTIFIERS),
        "generator_version": GENERATOR_VERSION,
        "schema_versions": dict(SCHEMA_VERSIONS),
        "track_schema_map": dict(TRACK_SCHEMA_MAP),
        "serialization_rules": dict(SERIALIZATION_RULES),
        "output_content_types": dict(OUTPUT_CONTENT_TYPES),
        "empty_release_policy": dict(EMPTY_RELEASE_POLICY),
        "source_entries_rule": dict(SOURCE_ENTRIES_RULE),
        "relation_rule": dict(RELATION_RULE),
    }
