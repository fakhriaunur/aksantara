"""Deterministic projection generator — word and relations artifacts.

Consumes validated release manifests and canonical entries deterministically.
No write path to canonical data; uses only caller-owned output roots.
Fixed inputs and clock produce byte-identical artifacts regardless of input
file order, with explicit relation semantics and witnesses.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from aksantara.domain.provenance import canonical_content_hash
from aksantara.projections.registry import GENERATOR_VERSION

__all__ = [
    "GENERATOR_VERSION",
    "artifact_bytes",
    "artifact_hash",
    "build_relations_artifact",
    "build_word_artifact",
    "serialize_artifact",
]


def _canonical_json_bytes(payload: Any) -> bytes:
    """Deterministic JSON serialization: sorted keys, compact, UTF-8, final newline."""
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return (text + "\n").encode("utf-8")


def artifact_bytes(records: list[dict[str, Any]]) -> bytes:
    """Serialize artifact records deterministically."""
    return _canonical_json_bytes(records)


def artifact_hash(data: bytes) -> str:
    """Lower-case hex SHA-256 of artifact bytes."""
    return hashlib.sha256(data).hexdigest()


def serialize_artifact(records: list[dict[str, Any]]) -> tuple[bytes, str]:
    """Return (bytes, hash) for artifact records."""
    b = artifact_bytes(records)
    return b, artifact_hash(b)


def _entry_source_hash(entry: Any) -> str:
    src = getattr(entry, "source", None)
    if src is not None:
        return getattr(src, "content_hash", "") or ""
    if isinstance(entry, dict):
        srcd = entry.get("source", {})
        return srcd.get("content_hash") or srcd.get("contentHash") or ""
    return ""


def _entry_source_url(entry: Any) -> str:
    src = getattr(entry, "source", None)
    if src is not None:
        return getattr(src, "url", "") or ""
    if isinstance(entry, dict):
        return entry.get("source", {}).get("url", "") or ""
    return ""


def _entry_source_kind(entry: Any) -> str:
    src = getattr(entry, "source", None)
    if src is not None:
        return getattr(src, "source_kind", "") or ""
    if isinstance(entry, dict):
        return entry.get("source", {}).get("source_kind", "") or ""
    return ""


def build_word_artifact(
    entries: dict[str, Any],
    source_release: str,
) -> list[dict[str, Any]]:
    """Build deterministic word records sorted by id.

    Each record carries stable word identity, source witnesses, and hashes.
    Input order does not affect output (sorted by id).
    """
    records: list[dict[str, Any]] = []
    for eid in sorted(entries.keys()):
        entry = entries[eid]
        # Extract fields
        if hasattr(entry, "model_dump"):
            data = entry.model_dump(mode="json")
            lema = data.get("lema", eid)
            kelas_kata = data.get("kelas_kata", [])
            bentuk_baku = data.get("bentuk_baku")
            bentuk_tidak_baku = data.get("bentuk_tidak_baku", [])
        elif isinstance(entry, dict):
            lema = entry.get("lema", eid)
            kelas_kata = entry.get("kelas_kata", [])
            bentuk_baku = entry.get("bentuk_baku")
            bentuk_tidak_baku = entry.get("bentuk_tidak_baku", [])
        else:
            lema = getattr(entry, "lema", eid)
            kelas_kata = getattr(entry, "kelas_kata", [])
            bentuk_baku = getattr(entry, "bentuk_baku", None)
            bentuk_tidak_baku = getattr(entry, "bentuk_tidak_baku", [])

        cch = canonical_content_hash(entry)
        rch = _entry_source_hash(entry)
        url = _entry_source_url(entry)
        kind = _entry_source_kind(entry)

        rec: dict[str, Any] = {
            "id": eid,
            "lema": lema,
            "source_entry_id": eid,
            "canonical_content_hash": cch,
            "raw_content_hash": rch,
            "source_url": url,
            "source_kind": kind,
            "source_release": source_release,
            "kelas_kata": sorted(kelas_kata)
            if isinstance(kelas_kata, list)
            else kelas_kata,
            "bentuk_baku": bentuk_baku,
            "bentuk_tidak_baku": sorted(bentuk_tidak_baku)
            if isinstance(bentuk_tidak_baku, list)
            else bentuk_tidak_baku,
        }
        records.append(rec)
    # Already sorted by id
    return records


def build_relations_artifact(
    entries: dict[str, Any],
    source_release: str,
) -> list[dict[str, Any]]:
    """Build deterministic relation records sorted by (from, to, type, source_entry_id).

    Each bentuk_tidak_baku element produces edge variant -> lema.
    Each bentuk_baku string produces edge lema -> bentuk_baku.
    Duplicates deduplicated; explicit direction, no invented endpoints.
    """
    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for eid in sorted(entries.keys()):
        entry = entries[eid]
        if hasattr(entry, "model_dump"):
            data = entry.model_dump(mode="json")
            lema = data.get("lema", eid)
            bentuk_baku = data.get("bentuk_baku")
            bentuk_tidak_baku = data.get("bentuk_tidak_baku", [])
        elif isinstance(entry, dict):
            lema = entry.get("lema", eid)
            bentuk_baku = entry.get("bentuk_baku")
            bentuk_tidak_baku = entry.get("bentuk_tidak_baku", [])
        else:
            lema = getattr(entry, "lema", eid)
            bentuk_baku = getattr(entry, "bentuk_baku", None)
            bentuk_tidak_baku = getattr(entry, "bentuk_tidak_baku", [])

        cch = canonical_content_hash(entry)

        # bentuk_tidak_baku: each variant -> lema
        if isinstance(bentuk_tidak_baku, list):
            for variant in sorted(set(bentuk_tidak_baku)):
                if not variant or not variant.strip():
                    continue
                key = (variant, lema, "nonstandard_variant")
                if key in seen:
                    continue
                seen.add(key)
                relations.append(
                    {
                        "from": variant,
                        "to": lema,
                        "type": "nonstandard_variant",
                        "source_entry_id": eid,
                        "canonical_field": "bentuk_tidak_baku",
                        "source_hash": cch,
                        "source_release": source_release,
                    }
                )

        # bentuk_baku: lema -> standard form
        if isinstance(bentuk_baku, str) and bentuk_baku.strip():
            key2 = (lema, bentuk_baku, "nonstandard_variant")
            if key2 not in seen:
                seen.add(key2)
                relations.append(
                    {
                        "from": lema,
                        "to": bentuk_baku,
                        "type": "nonstandard_variant",
                        "source_entry_id": eid,
                        "canonical_field": "bentuk_baku",
                        "source_hash": cch,
                        "source_release": source_release,
                    }
                )

    # Deterministic sort
    relations.sort(key=lambda r: (r["from"], r["to"], r["type"], r["source_entry_id"]))
    return relations


def build_artifact_for_track(
    track: str,
    entries: dict[str, Any],
    source_release: str,
) -> list[dict[str, Any]]:
    if track == "word":
        return build_word_artifact(entries, source_release)
    elif track == "relations":
        return build_relations_artifact(entries, source_release)
    else:
        raise ValueError(f"unsupported track: {track}")
