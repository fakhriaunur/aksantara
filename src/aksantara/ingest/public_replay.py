"""Public, read-only replay of caller-owned KBBI snapshot bytes.

Replay is intentionally separate from checkpoint execution.  It reads one
caller-owned file, verifies its bytes before parsing, and returns the
deterministic canonical record.  It never repairs state, writes artifacts,
uses a network transport, or invokes an LLM.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from aksantara.domain.errors import AksantaraDomainError
from aksantara.domain.models import SourceRef
from aksantara.domain.provenance import (
    CANONICAL_RECORD_FIELDS,
    canonical_content_hash,
    canonical_record_bytes,
    content_hash_bytes,
)
from aksantara.ingest.checkpoint_types import AUTHORITY_POLICY_VERSION
from aksantara.parse.parser_contract import PARSER_VERSION, ParserError, parse_kbbi
from aksantara.validate.schema import validate_entry

KNOWN_RAW_HASHES: dict[str, str] = {
    "februari": "35a7028aa2ef140e54ea9a783ee0c87e9e79729ed51e914352e14ee099d703c5"
}
TRANSFORM_VERSION = "0.1.0"
VALIDATION_POLICY_VERSION = AUTHORITY_POLICY_VERSION
_KEY_RE = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,256}$")


class ReplayError(ValueError):
    """Structured non-success for a rejected public replay."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        status_code: int = 422,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            value["details"] = self.details
        return value


def _normalise_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized):
        raise ReplayError(
            "replay_key_invalid",
            "stable key contains a control character",
        )
    key = " ".join(normalized.strip().split()).casefold()
    if not key or not _KEY_RE.fullmatch(key) or ".." in key:
        raise ReplayError(
            "replay_key_invalid",
            "stable key is blank or contains unsafe characters",
        )
    return key


def _key_from_source(source_ref: SourceRef) -> str:
    """Return the normalized entry identity encoded by an official URL.

    ``SourceRef`` deliberately validates its data shape, not URL syntax.
    Replay is a public boundary, so URL parsing errors must become a
    machine-readable replay error instead of escaping as ``ValueError`` from
    ``urlsplit`` (notably for malformed bracketed hosts).
    """
    try:
        if any(
            character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}
            for character in source_ref.url
        ) or re.search(r"%(?![0-9A-Fa-f]{2})", source_ref.url):
            raise ValueError("invalid URL characters")
        parsed_url = urlsplit(source_ref.url)
        # Accessing hostname/port performs additional validation and can raise
        # ValueError for malformed bracketed hosts or ports.
        hostname = parsed_url.hostname
        _ = parsed_url.port
    except (UnicodeError, ValueError) as exc:
        raise ReplayError(
            "replay_source_ref_invalid",
            "source reference URL is malformed",
            details={"error_type": type(exc).__name__},
        ) from exc
    if (
        parsed_url.scheme != "https"
        or hostname is None
        or hostname.lower() != "kbbi.kemdikbud.go.id"
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise ReplayError(
            "replay_official_source_required",
            "public replay requires the official KBBI HTTPS host",
        )
    path_part = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
    if not path_part:
        raise ReplayError(
            "replay_source_ref_invalid",
            "source reference URL must name a KBBI entry",
        )
    return _normalise_key(unquote(path_part))


def _resolve_raw_path(root: Path, raw_path: str | Path) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReplayError(
            "replay_path_escape",
            "raw snapshot path escapes caller root",
            details={"root": str(root), "raw_path": str(raw_path)},
        ) from exc
    return resolved


def replay_snapshot(
    *,
    root: Path | str,
    raw_path: Path | str,
    source_ref: SourceRef,
    expected_raw_hash: str,
    expected_canonical_hash: str | None = None,
    stable_key: str | None = None,
    parser_version: str = PARSER_VERSION,
    transform_version: str = TRANSFORM_VERSION,
    validation_policy: str = VALIDATION_POLICY_VERSION,
) -> dict[str, Any]:
    """Replay one snapshot with hash-first, read-only deterministic semantics."""
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ReplayError(
            "replay_root_not_found",
            "caller root does not exist or is not a directory",
            details={"root": str(root_path)},
            status_code=404,
        )
    if parser_version != PARSER_VERSION:
        raise ReplayError(
            "replay_parser_pin_mismatch",
            "parser_version is not the pinned parser version",
            details={"expected": PARSER_VERSION, "requested": parser_version},
        )
    if transform_version != TRANSFORM_VERSION:
        raise ReplayError(
            "replay_transform_pin_mismatch",
            "transform_version is not the pinned transform version",
            details={"expected": TRANSFORM_VERSION, "requested": transform_version},
        )
    if validation_policy != VALIDATION_POLICY_VERSION:
        raise ReplayError(
            "replay_policy_pin_mismatch",
            "validation_policy is not the pinned validation policy",
            details={
                "expected": VALIDATION_POLICY_VERSION,
                "requested": validation_policy,
            },
        )
    source_key = _key_from_source(source_ref)
    key = _normalise_key(stable_key) if stable_key is not None else source_key
    if stable_key is not None and key != source_key:
        raise ReplayError(
            "replay_source_identity_mismatch",
            "stable_key does not match the entry identity in SourceRef.url",
            details={
                "stable_key": key,
                "source_key": source_key,
                "source_url": source_ref.url,
            },
        )
    if not isinstance(expected_raw_hash, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_raw_hash
    ):
        raise ReplayError(
            "replay_expected_hash_invalid",
            "expected_raw_hash must be a 64-character hexadecimal SHA-256",
        )
    expected_hash = expected_raw_hash.lower()
    path = _resolve_raw_path(root_path, raw_path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise ReplayError(
            "replay_raw_not_found",
            "raw snapshot was not found",
            details={"raw_path": str(raw_path)},
            status_code=404,
        ) from exc
    except OSError as exc:
        raise ReplayError(
            "replay_raw_unreadable",
            "raw snapshot could not be read",
            details={"error_type": type(exc).__name__},
            status_code=422,
        ) from exc
    actual_hash = content_hash_bytes(raw_bytes)
    if actual_hash != expected_hash:
        raise ReplayError(
            "replay_raw_hash_mismatch",
            "raw snapshot hash does not match the expected pin",
            details={"expected": expected_hash, "actual": actual_hash},
        )
    if source_ref.content_hash != actual_hash:
        raise ReplayError(
            "replay_source_hash_mismatch",
            "SourceRef content_hash does not match the verified raw bytes",
            details={
                "source_ref": source_ref.content_hash,
                "actual": actual_hash,
            },
        )
    if source_ref.source_kind not in {"official-live", "official-snapshot"}:
        raise ReplayError(
            "replay_official_source_required",
            "public replay requires an official KBBI source reference",
        )
    if source_ref.parser_version != parser_version:
        raise ReplayError(
            "replay_parser_pin_mismatch",
            "SourceRef parser_version does not match the replay pin",
            details={
                "source_ref": source_ref.parser_version,
                "requested": parser_version,
            },
        )
    try:
        entry = parse_kbbi(raw_bytes, source_ref)
        validate_entry(entry, raw_bytes=raw_bytes)
    except (AksantaraDomainError, ParserError, ValueError, TypeError) as exc:
        raise ReplayError(
            "replay_parse_error",
            "raw snapshot did not produce a valid canonical entry",
            details={"error_type": type(exc).__name__, "message": str(exc)},
        ) from exc
    if _normalise_key(entry.id) != key and _normalise_key(entry.lema) != key:
        raise ReplayError(
            "replay_key_mismatch",
            "parsed entry identity does not match the requested stable key",
            details={"stable_key": key, "entry_id": entry.id, "lema": entry.lema},
        )
    if entry.parser_version != parser_version:
        raise ReplayError(
            "replay_parser_pin_mismatch",
            "parsed entry parser_version does not match the replay pin",
            details={"actual": entry.parser_version, "expected": parser_version},
        )
    if entry.transform_version != transform_version:
        raise ReplayError(
            "replay_transform_pin_mismatch",
            "parsed entry transform_version does not match the replay pin",
            details={"actual": entry.transform_version, "expected": transform_version},
        )
    published_bytes = canonical_record_bytes(entry)
    canonical_hash = canonical_content_hash(entry)
    if expected_canonical_hash is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_canonical_hash):
            raise ReplayError(
                "replay_canonical_hash_invalid",
                "expected_canonical_hash must be a 64-character hexadecimal SHA-256",
            )
        if canonical_hash != expected_canonical_hash.lower():
            raise ReplayError(
                "replay_canonical_hash_mismatch",
                "canonical record hash does not match the expected pin",
                details={
                    "expected": expected_canonical_hash.lower(),
                    "actual": canonical_hash,
                },
            )
    source_payload = source_ref.model_dump(mode="json")
    return {
        "schema_version": "public-replay-v1",
        "stable_key": key,
        "deterministic": True,
        "read_only": True,
        "raw": {
            "path": str(path.relative_to(root_path)),
            "bytes": len(raw_bytes),
            "raw_content_hash": actual_hash,
            "expected_raw_hash": expected_hash,
        },
        "canonical": {
            "entry": entry.model_dump(mode="json"),
            "canonical_content_hash": canonical_hash,
            "canonical_hash": canonical_hash,
            "bytes": len(published_bytes),
            "serialization": {
                "algorithm": "canonical-record-v1",
                "fields": list(CANONICAL_RECORD_FIELDS),
                "encoding": "UTF-8",
                "sort_keys": True,
                "separators": [",", ":"],
                "final_newline": True,
            },
        },
        "source_ref": source_payload,
        "pins": {
            "parser_version": parser_version,
            "transform_version": transform_version,
            "validation_policy": validation_policy,
        },
        "transport": {
            "mode": "caller-owned-file-only",
            "live_network_attempts": 0,
            "llm_calls": 0,
        },
        "writes": {"count": 0, "paths": []},
    }


__all__ = [
    "KNOWN_RAW_HASHES",
    "TRANSFORM_VERSION",
    "VALIDATION_POLICY_VERSION",
    "ReplayError",
    "replay_snapshot",
]
