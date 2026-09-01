"""Generic word/relations projection schemas (v1).

Publishes UTF-8/key/array/number/final-newline rules, stable unique word
IDs/lemmas/source witnesses, and relation from/to/type/source entry/
canonical field/source hash with explicit direction and duplicate handling.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "RELATIONS_SCHEMA_V1",
    "RELATION_REQUIRED_FIELDS",
    "SERIALIZATION_CONTRACT",
    "WORD_REQUIRED_FIELDS",
    "WORD_SCHEMA_V1",
    "validate_relation_record",
    "validate_word_record",
]

SERIALIZATION_CONTRACT: dict[str, str] = {
    "encoding": "UTF-8 without BOM",
    "json_keys": "sorted keys (sort_keys=True)",
    "json_separators": "(',', ':') compact",
    "json_ensure_ascii": "False",
    "array_order": "words sorted by id; relations sorted by (from, to, type)",
    "number_format": "JSON finite numbers only",
    "final_newline": "exactly one \\n at end of file",
    "hash": "lower-case hex SHA-256 of UTF-8 bytes of canonical JSON",
}

WORD_SCHEMA_V1: dict[str, Any] = {
    "schema_version": "word-v1",
    "track": "word",
    "description": "Generic word list projection — every eligible release entry as a word record",
    "record_fields": {
        "id": "stable entry id (string, required, unique)",
        "lema": "headword as shown in KBBI (string, required)",
        "source_entry_id": "canonical entry id (string, required, equals id)",
        "canonical_content_hash": "hex SHA-256 of canonical record bytes (string, 64 hex, required)",
        "raw_content_hash": "hex SHA-256 of raw snapshot bytes (string, 64 hex, required)",
        "source_url": "canonical source URL (string, required)",
        "source_kind": "source kind, must be official-live or official-snapshot (string, required)",
        "source_release": "release version that provided this entry (string, required)",
        "kelas_kata": "word class labels (list of strings)",
        "bentuk_baku": "standard form if variant (string or null)",
        "bentuk_tidak_baku": "nonstandard variants (list of strings)",
    },
    "required": [
        "id",
        "lema",
        "source_entry_id",
        "canonical_content_hash",
        "raw_content_hash",
        "source_url",
        "source_kind",
        "source_release",
    ],
    "ordering": "records sorted by id (lexicographic)",
    "content_type": "application/json",
    "serialization": dict(SERIALIZATION_CONTRACT),
}

RELATIONS_SCHEMA_V1: dict[str, Any] = {
    "schema_version": "relations-v1",
    "track": "relations",
    "description": "Generic relations projection — directed nonstandard→standard edges derived from bentuk_tidak_baku and bentuk_baku",
    "record_fields": {
        "from": "nonstandard form string (required)",
        "to": "standard form string (required)",
        "type": "relation type, always 'nonstandard_variant' (required)",
        "source_entry_id": "canonical entry id that declares this relation (required)",
        "canonical_field": "which field produced this edge: 'bentuk_tidak_baku' or 'bentuk_baku' (required)",
        "source_hash": "canonical_content_hash of the source entry (required, 64 hex)",
        "source_release": "release version (required)",
    },
    "required": [
        "from",
        "to",
        "type",
        "source_entry_id",
        "canonical_field",
        "source_hash",
        "source_release",
    ],
    "ordering": "records sorted by (from, to, type, source_entry_id)",
    "content_type": "application/json",
    "serialization": dict(SERIALIZATION_CONTRACT),
    "rules": {
        "direction": "explicit directed edge from nonstandard to standard",
        "endpoint_lookup": "to endpoint resolved within same release set; unresolved still emitted as declared string",
        "unresolved_target": "no synthetic entry created; from/to are literal strings from canonical fields",
        "normalization": "forms used as-is from canonical entry, no case folding",
        "duplicate_key": "duplicate (from,to,type) tuples deduplicated, first source_entry wins deterministically",
        "bentuk_tidak_baku_vs_bentuk_baku": "bentuk_tidak_baku list elements each produce one edge variant->lema; bentuk_baku string produces one edge lema->bentuk_baku; both explicit, no implied direction",
    },
}

WORD_REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "lema",
    "source_entry_id",
    "canonical_content_hash",
    "raw_content_hash",
    "source_url",
    "source_kind",
    "source_release",
)
RELATION_REQUIRED_FIELDS: tuple[str, ...] = (
    "from",
    "to",
    "type",
    "source_entry_id",
    "canonical_field",
    "source_hash",
    "source_release",
)


def validate_word_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in WORD_REQUIRED_FIELDS:
        if (
            field not in record
            or record[field] is None
            or (isinstance(record[field], str) and not record[field].strip())
        ):
            errors.append(f"missing required field: {field}")
    if "canonical_content_hash" in record and record.get("canonical_content_hash"):
        h = record["canonical_content_hash"]
        if (
            not isinstance(h, str)
            or len(h) != 64
            or not all(c in "0123456789abcdef" for c in h.lower())
        ):
            errors.append("canonical_content_hash must be 64 hex chars")
    if "raw_content_hash" in record and record.get("raw_content_hash"):
        h = record["raw_content_hash"]
        if (
            not isinstance(h, str)
            or len(h) != 64
            or not all(c in "0123456789abcdef" for c in h.lower())
        ):
            errors.append("raw_content_hash must be 64 hex chars")
    return errors


def validate_relation_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in RELATION_REQUIRED_FIELDS:
        if (
            field not in record
            or record[field] is None
            or (isinstance(record[field], str) and not record[field].strip())
        ):
            errors.append(f"missing required field: {field}")
    if record.get("type") and record["type"] != "nonstandard_variant":
        errors.append(
            f"relation type must be 'nonstandard_variant', got {record['type']!r}"
        )
    if record.get("canonical_field") and record["canonical_field"] not in (
        "bentuk_tidak_baku",
        "bentuk_baku",
    ):
        errors.append(
            f"canonical_field must be bentuk_tidak_baku or bentuk_baku, got {record['canonical_field']!r}"
        )
    if "source_hash" in record and record.get("source_hash"):
        h = record["source_hash"]
        if (
            not isinstance(h, str)
            or len(h) != 64
            or not all(c in "0123456789abcdef" for c in h.lower())
        ):
            errors.append("source_hash must be 64 hex chars")
    return errors
