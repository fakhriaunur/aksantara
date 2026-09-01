"""Firestore helper utilities for VectorRecord batching."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aksantara.embeddings.firestore_types import VECTOR_FIELD, VectorRecord

__all__ = [
    "_wrap_vector",
    "_unwrap_vector",
    "_record_doc_id",
    "_record_fingerprint",
    "_deduplicate_records",
    "_record_payload",
]


def _wrap_vector(value: list[float]) -> Any:
    try:
        from google.cloud.firestore import Vector  # type: ignore[import-untyped]

        return Vector(value)
    except Exception:
        return value


def _unwrap_vector(value: Any) -> list[float]:
    try:
        from google.cloud.firestore import Vector  # type: ignore[import-untyped]

        if isinstance(value, Vector):
            return list(value)
    except Exception:
        pass
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    return []


def _record_doc_id(record: VectorRecord) -> str:
    if not (record.id or record.entry_id):
        raise ValueError("vector record requires id or entry_id")
    return record.doc_id or record.entry_id or record.id


def _record_fingerprint(record: VectorRecord) -> dict[str, Any]:
    payload = record.to_firestore_dict()
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    return payload


def _deduplicate_records(records: Iterable[VectorRecord]) -> list[VectorRecord]:
    unique: list[VectorRecord] = []
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        doc_id = _record_doc_id(record)
        fingerprint = _record_fingerprint(record)
        previous = seen.get(doc_id)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError(f"conflicting records for document id {doc_id!r}")
            continue
        seen[doc_id] = fingerprint
        unique.append(record)
    return unique


def _record_payload(record: VectorRecord) -> dict[str, Any]:
    payload = record.to_firestore_dict()
    vec_list = record.vector_as_list()
    try:
        from google.cloud.firestore_v1.vector import (
            Vector,  # type: ignore[import-not-found]
        )

        payload[VECTOR_FIELD] = Vector(vec_list)
        payload["embedding"] = Vector(vec_list)
    except Exception:
        payload[VECTOR_FIELD] = _wrap_vector(vec_list)
    return payload
