"""Caller-rooted atomic and immutable checkpoint storage helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from aksantara.domain.provenance import content_hash_bytes
from aksantara.ingest.checkpoint_types import (
    CheckpointNotFoundError,
    CheckpointPersistenceError,
)


def _canonical_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _hash_payload(payload: Any) -> str:
    return content_hash_bytes(_canonical_bytes(payload)[:-1])


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckpointNotFoundError(
            "durable checkpoint artifact was not found",
            details={"path": str(path)},
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointPersistenceError(
            "durable checkpoint artifact could not be read",
            details={"path": str(path), "error_type": type(exc).__name__},
        ) from exc
    if not isinstance(value, dict):
        raise CheckpointPersistenceError(
            "durable checkpoint artifact must contain an object",
            details={"path": str(path)},
        )
    return value


def _write_json(path: Path, payload: Any, root: Path) -> None:
    _write_immutable(path, _canonical_bytes(payload), root)


def _write_state_json(path: Path, payload: Any, root: Path) -> None:
    """Atomically replace a mutable current-state document.

    State documents are accompanied by immutable revisioned outcome/attempt
    artifacts.  Replacing only this small pointer-like document keeps status
    reads coherent without pretending that a mutable status path is history.
    """
    data = _canonical_bytes(payload)
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise CheckpointPersistenceError(
            "state path escapes caller root",
            details={"path": str(path), "root": str(root)},
        ) from exc
    temporary = path.with_name(f".{path.name}.{os.getpid()}.state.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CheckpointPersistenceError(
            "caller-owned state write failed",
            details={"path": str(path), "error_type": type(exc).__name__},
        ) from exc


def _write_immutable(path: Path, data: bytes, root: Path) -> None:
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise CheckpointPersistenceError(
            "artifact path escapes caller root",
            details={"path": str(path), "root": str(root)},
        ) from exc
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if existing != data:
                raise CheckpointPersistenceError(
                    "immutable artifact identity already contains different bytes",
                    details={"path": str(path)},
                )
            return
        if temporary.exists():
            temporary.unlink()
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except CheckpointPersistenceError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CheckpointPersistenceError(
            "caller-owned artifact write failed",
            details={"path": str(path), "error_type": type(exc).__name__},
        ) from exc


def _safe_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise CheckpointPersistenceError(
            "artifact reference escapes caller root",
            details={"path": str(path), "root": str(root)},
        ) from exc


def _redact_catalog_request(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Persist request identity without duplicating potentially large bytes."""

    def redact(value: Any, *, key: str | None = None) -> Any:
        if key in {"bytes", "raw_bytes", "content", "base64"}:
            return "<bound-immutable-bytes>"
        if isinstance(value, (datetime, date)):
            return value.isoformat().replace("+00:00", "Z")
        if hasattr(value, "model_dump"):
            return redact(value.model_dump(mode="json"), key=key)
        if isinstance(value, Mapping):
            return {
                str(child_key): redact(child_value, key=str(child_key))
                for child_key, child_value in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, tuple):
            return [redact(item) for item in value]
        return value

    result = redact(catalog)
    if not isinstance(result, dict):
        raise CheckpointPersistenceError("catalog request redaction failed")
    return result


__all__ = [
    "_canonical_bytes",
    "_hash_payload",
    "_read_json",
    "_redact_catalog_request",
    "_safe_relative",
    "_write_immutable",
    "_write_json",
    "_write_state_json",
]
