"""Pure validation and deterministic selection for checkpoint catalogs."""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError as PydanticValidationError

from aksantara.domain.authority import SourceKind
from aksantara.domain.models import SourceRef
from aksantara.ingest.checkpoint_types import (
    _ALLOWED_CONTENT_TYPES,
    _ALLOWED_FIXTURE_ADAPTERS,
    _ALLOWED_SOURCE_KINDS,
    _CONTROL_RE,
    _FALLBACK_HOSTS,
    _HASH_RE,
    _OFFICIAL_HOSTS,
    _OFFICIAL_SOURCE_KINDS,
    AUTHORITY_POLICY_VERSION,
    COMPARISON_POLICY_VERSION,
    DEFAULT_LIMIT,
    MAX_KEY_LENGTH,
    MAX_LIMIT,
    TRANSFORM_VERSION,
    CatalogValidationError,
    LimitValidationError,
    _CatalogRecord,
)
from aksantara.parse.parser_contract import PARSER_VERSION


def _string_value(
    payload: Mapping[str, Any],
    *names: str,
    required: bool = True,
    default: str | None = None,
) -> str | None:
    for name in names:
        if name in payload:
            value = payload[name]
            if not isinstance(value, str):
                raise CatalogValidationError(
                    f"{name} must be a string",
                    details={"field": name},
                )
            value = value.strip()
            if value:
                return value
            if required:
                raise CatalogValidationError(
                    f"{name} must not be blank",
                    details={"field": name},
                )
            return default
    if required:
        raise CatalogValidationError(
            f"missing required field {names[0]}",
            details={"field": names[0]},
        )
    return default


def normalize_stable_key(value: str) -> str:
    """Normalize a catalog key without permitting a filesystem-like key.

    Normalization is Unicode NFKC, whitespace collapse, and Unicode
    case-folding.  Keys are identifiers, never paths, URLs, or filesystem
    selectors.
    """
    if not isinstance(value, str):
        raise CatalogValidationError("stable_key must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    if _CONTROL_RE.search(normalized) or any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in normalized
    ):
        raise CatalogValidationError("stable_key contains a control character")
    normalized = " ".join(normalized.split()).casefold()
    if not normalized:
        raise CatalogValidationError("stable_key must not be blank")
    if len(normalized) > MAX_KEY_LENGTH:
        raise CatalogValidationError(
            "stable_key exceeds maximum length",
            details={"max_length": MAX_KEY_LENGTH},
        )
    if (
        "/" in normalized
        or "\\" in normalized
        or normalized in {".", ".."}
        or normalized.startswith(("./", "../"))
        or normalized.endswith(("/.", "/.."))
        or ".." in normalized
    ):
        raise CatalogValidationError(
            "stable_key must not be path-like",
            details={"stable_key": value},
        )
    if normalized.startswith(("gs://", "http://", "https://")):
        raise CatalogValidationError("stable_key must not be a URL")
    return normalized


def _validate_limit(value: int | str | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool):
        raise LimitValidationError("limit must be an integer", details={"limit": value})
    if isinstance(value, str):
        stripped = value.strip()
        if not re.fullmatch(r"[+-]?[0-9]+", stripped):
            raise LimitValidationError(
                "limit must be an integer",
                details={"limit": value},
            )
        try:
            value = int(stripped)
        except ValueError as exc:
            raise LimitValidationError(
                "limit must be an integer",
                details={"limit": value},
            ) from exc
    if not isinstance(value, int):
        raise LimitValidationError("limit must be an integer")
    if value < 1 or value > MAX_LIMIT:
        raise LimitValidationError(
            "limit must be in the inclusive range 1..100",
            details={"limit": value, "min": 1, "max": MAX_LIMIT},
        )
    return value


def selection_keys(
    records: list[str] | tuple[str, ...], limit: int = DEFAULT_LIMIT
) -> list[str]:
    """Return exactly the stable sorted prefix admitted by ``limit``."""
    effective_limit = _validate_limit(limit)
    normalized = [normalize_stable_key(record) for record in records]
    if len(normalized) != len(set(normalized)):
        raise CatalogValidationError("normalized stable_key collision")
    return sorted(normalized)[:effective_limit]


def _parse_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CatalogValidationError(
                f"{field} must be an ISO-8601 timestamp",
                details={"field": field},
            ) from exc
    else:
        raise CatalogValidationError(
            f"{field} must be an ISO-8601 timestamp",
            details={"field": field},
        )
    if parsed.tzinfo is None:
        raise CatalogValidationError(
            f"{field} must include a timezone",
            details={"field": field},
        )
    return parsed.astimezone(UTC)


def _validate_hash(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise CatalogValidationError(
            f"{field} must be 64 lower-case hexadecimal characters",
            details={"field": field},
        )
    return value


def _parse_source_ref(payload: object, stable_key: str) -> SourceRef:
    if isinstance(payload, SourceRef):
        source_ref = payload
    elif isinstance(payload, Mapping):
        url = _string_value(payload, "url", "source_url")
        source_kind = _string_value(payload, "source_kind", "sourceKind")
        edition = _string_value(
            payload,
            "edition",
            required=False,
            default="VI",
        )
        source_version = _string_value(
            payload,
            "source_version",
            "sourceVersion",
            required=False,
            default="VI",
        )
        retrieved_at_value: object = payload.get(
            "retrieved_at",
            payload.get("retrievedAt"),
        )
        if retrieved_at_value is None:
            raise CatalogValidationError(
                "source_ref.retrieved_at is required",
                details={"field": "source_ref.retrieved_at"},
            )
        content_hash_value = payload.get(
            "content_hash",
            payload.get("contentHash"),
        )
        content_hash = _validate_hash(
            content_hash_value,
            field="source_ref.content_hash",
        )
        parser_version = _string_value(
            payload,
            "parser_version",
            "parserVersion",
            required=False,
            default=PARSER_VERSION,
        )
        if (
            url is None
            or source_kind is None
            or edition is None
            or source_version is None
            or parser_version is None
        ):
            raise CatalogValidationError("source_ref contains a missing field")
        try:
            source_ref = SourceRef(
                url=url,
                source_kind=cast(SourceKind, source_kind),
                edition=edition,
                source_version=source_version,
                retrieved_at=_parse_datetime(
                    retrieved_at_value,
                    field="source_ref.retrieved_at",
                ),
                content_hash=content_hash,
                parser_version=parser_version,
            )
        except PydanticValidationError as exc:
            raise CatalogValidationError(
                "source_ref is invalid",
                details={
                    "stable_key": stable_key,
                    "error_type": type(exc).__name__,
                },
            ) from exc
    else:
        raise CatalogValidationError(
            "each entry requires a source_ref object",
            details={"stable_key": stable_key},
        )

    if source_ref.source_kind not in _ALLOWED_SOURCE_KINDS:
        raise CatalogValidationError(
            "source_ref.source_kind is not recognized",
            details={"source_kind": source_ref.source_kind},
        )
    if source_ref.parser_version != PARSER_VERSION:
        raise CatalogValidationError(
            "source_ref.parser_version does not match the pinned parser",
            details={
                "expected": PARSER_VERSION,
                "actual": source_ref.parser_version,
            },
        )
    parsed_url = urlsplit(source_ref.url)
    host = (parsed_url.hostname or "").lower()
    allowed_hosts = _OFFICIAL_HOSTS | _FALLBACK_HOSTS
    if parsed_url.scheme != "https" or host not in allowed_hosts:
        raise CatalogValidationError(
            "source_ref.url must use an approved HTTPS source host",
            details={"host": host, "allowed_hosts": sorted(allowed_hosts)},
        )
    if source_ref.source_kind in _OFFICIAL_SOURCE_KINDS and host not in _OFFICIAL_HOSTS:
        raise CatalogValidationError(
            "official source_ref must use the official KBBI host",
            details={"host": host},
        )
    if source_ref.source_kind in {"fallback", "gov-derived"} and host not in (
        _OFFICIAL_HOSTS | _FALLBACK_HOSTS
    ):
        raise CatalogValidationError(
            "fallback source_ref host is not approved",
            details={"host": host},
        )
    path_tail = unquote(parsed_url.path.rstrip("/").rsplit("/", 1)[-1])
    if not path_tail or normalize_stable_key(path_tail) != stable_key:
        raise CatalogValidationError(
            "source_ref URL identity does not match stable_key",
            details={"stable_key": stable_key, "url": source_ref.url},
        )
    return source_ref


def _transport_dict(payload: object, root: Path, stable_key: str) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = {"adapter": "fixture", "path": payload}
    if not isinstance(payload, Mapping):
        raise CatalogValidationError(
            "transport must be an object",
            details={"stable_key": stable_key},
        )
    adapter = _string_value(payload, "adapter", "adapter_name")
    if adapter not in _ALLOWED_FIXTURE_ADAPTERS:
        raise CatalogValidationError(
            "only the caller-owned fixture adapter is allowed in local mode",
            details={"adapter": adapter, "allowed": sorted(_ALLOWED_FIXTURE_ADAPTERS)},
        )
    status_value = payload.get("status", 200)
    if isinstance(status_value, bool) or not isinstance(status_value, int):
        raise CatalogValidationError("transport.status must be an integer")
    if status_value < 100 or status_value > 599:
        raise CatalogValidationError("transport.status must be between 100 and 599")
    content_type = _string_value(
        payload,
        "content_type",
        "contentType",
        required=False,
        default="text/html",
    )
    if content_type is None:
        raise CatalogValidationError("transport.content_type is required")
    content_type = content_type.split(";", 1)[0].strip().lower()
    if status_value < 400 and content_type not in _ALLOWED_CONTENT_TYPES:
        raise CatalogValidationError(
            "transport.content_type is not supported",
            details={"content_type": content_type},
        )
    comparison_mode = _string_value(
        payload,
        "comparison_mode",
        "comparisonMode",
        required=False,
        default="exact",
    )
    if comparison_mode not in {"exact", "sha256"}:
        raise CatalogValidationError(
            "transport.comparison_mode must be exact or sha256",
            details={"comparison_mode": comparison_mode},
        )
    expected_raw_hash_value = payload.get(
        "expected_raw_hash",
        payload.get("expectedRawHash", payload.get("content_hash")),
    )
    if expected_raw_hash_value is None and status_value >= 400:
        expected_raw_hash = ""
    else:
        expected_raw_hash = _validate_hash(
            expected_raw_hash_value,
            field="transport.expected_raw_hash",
        )
    source_hash = expected_raw_hash or None
    if source_hash is not None:
        # The two identities must agree before any source read is attempted.
        # The bytes are still checked again during execution.
        # SourceRef is reconstructed above and available through caller later.
        pass

    binding_names = [
        name
        for name in ("path", "bytes", "raw_bytes", "content", "base64")
        if name in payload
    ]
    if len(binding_names) > 1:
        raise CatalogValidationError(
            "transport must bind exactly one immutable fixture representation",
            details={"fields": binding_names, "stable_key": stable_key},
        )
    result: dict[str, Any] = {
        "adapter": adapter,
        "status": status_value,
        "content_type": content_type,
        "comparison_mode": comparison_mode,
        "expected_raw_hash": expected_raw_hash,
    }
    if "path" in payload:
        path_value = payload["path"]
        if not isinstance(path_value, str) or not path_value.strip():
            raise CatalogValidationError("transport.path must be a non-empty string")
        relative = Path(path_value)
        if relative.is_absolute() or _CONTROL_RE.search(path_value):
            raise CatalogValidationError(
                "transport.path must be a safe relative path",
                details={"path": path_value},
            )
        try:
            resolved = (root / relative).resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise CatalogValidationError(
                "transport.path escapes caller root",
                details={"path": path_value, "root": str(root)},
            ) from exc
        if status_value < 400 and not resolved.is_file():
            raise CatalogValidationError(
                "transport.path must reference an existing fixture file",
                details={"path": path_value, "root": str(root)},
            )
        result["path"] = relative.as_posix()
    elif "bytes" in payload or "raw_bytes" in payload:
        byte_value = payload.get("bytes", payload.get("raw_bytes"))
        if not isinstance(byte_value, bytes):
            raise CatalogValidationError(
                "transport.bytes must be bytes for Python callers; use base64 in JSON"
            )
        result["bytes"] = byte_value
    elif "content" in payload:
        content = payload["content"]
        if not isinstance(content, str):
            raise CatalogValidationError("transport.content must be a UTF-8 string")
        result["bytes"] = content.encode("utf-8")
    elif "base64" in payload:
        encoded = payload["base64"]
        if not isinstance(encoded, str):
            raise CatalogValidationError("transport.base64 must be a string")
        try:
            result["bytes"] = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CatalogValidationError(
                "transport.base64 is not valid base64"
            ) from exc
    elif status_value < 400:
        raise CatalogValidationError(
            "successful fixture transport must bind path or immutable bytes",
            details={"stable_key": stable_key},
        )
    return result


def _catalog_records(
    catalog: Mapping[str, Any],
    *,
    root: Path,
) -> tuple[str, str, tuple[_CatalogRecord, ...], dict[str, Any]]:
    catalog_id = _string_value(catalog, "catalog_id", "catalogId", "id")
    corpus_version = _string_value(
        catalog,
        "corpus_version",
        "corpusVersion",
    )
    if catalog_id is None or corpus_version is None:
        raise CatalogValidationError("catalog identity is incomplete")
    if _CONTROL_RE.search(catalog_id) or _CONTROL_RE.search(corpus_version):
        raise CatalogValidationError("catalog identity contains a control character")
    entries_value = catalog.get("entries", catalog.get("records", catalog.get("items")))
    if not isinstance(entries_value, list):
        raise CatalogValidationError(
            "catalog.entries must be an array",
            details={"field": "entries"},
        )
    pins_value = catalog.get("pins", {})
    if pins_value is None:
        pins_value = {}
    if not isinstance(pins_value, Mapping):
        raise CatalogValidationError("catalog.pins must be an object")
    parser_pin = _string_value(
        pins_value,
        "parser_version",
        "parserVersion",
        required=False,
        default=PARSER_VERSION,
    )
    transform_pin = _string_value(
        pins_value,
        "transform_version",
        "transformVersion",
        required=False,
        default=TRANSFORM_VERSION,
    )
    validation_policy = _string_value(
        pins_value,
        "validation_policy",
        "validationPolicy",
        required=False,
        default=AUTHORITY_POLICY_VERSION,
    )
    if parser_pin != PARSER_VERSION:
        raise CatalogValidationError(
            "catalog parser pin does not match the installed parser",
            details={"expected": PARSER_VERSION, "actual": parser_pin},
        )
    if transform_pin != TRANSFORM_VERSION:
        raise CatalogValidationError(
            "catalog transform pin is not supported",
            details={"expected": TRANSFORM_VERSION, "actual": transform_pin},
        )
    if validation_policy != AUTHORITY_POLICY_VERSION:
        raise CatalogValidationError(
            "catalog validation policy is not supported",
            details={"expected": AUTHORITY_POLICY_VERSION, "actual": validation_policy},
        )
    records: list[_CatalogRecord] = []
    seen: dict[str, int] = {}
    for ordinal, raw_record in enumerate(entries_value):
        if not isinstance(raw_record, Mapping):
            raise CatalogValidationError(
                "catalog entry must be an object",
                details={"ordinal": ordinal},
            )
        raw_key = _string_value(
            raw_record,
            "stable_key",
            "stableKey",
            "key",
            "id",
            "lema",
        )
        if raw_key is None:
            raise CatalogValidationError("catalog entry has no stable key")
        stable_key = normalize_stable_key(raw_key)
        if stable_key in seen:
            raise CatalogValidationError(
                "normalized stable_key collision",
                details={
                    "stable_key": stable_key,
                    "first_ordinal": seen[stable_key],
                    "second_ordinal": ordinal,
                },
            )
        seen[stable_key] = ordinal
        source_payload = raw_record.get(
            "source_ref",
            raw_record.get("sourceRef", raw_record.get("source")),
        )
        source_ref = _parse_source_ref(source_payload, stable_key)
        transport_payload = raw_record.get(
            "transport",
            raw_record.get("fixture", raw_record.get("snapshot")),
        )
        if transport_payload is None and any(
            key in raw_record for key in ("raw_bytes", "bytes", "content", "base64")
        ):
            transport_payload = {
                key: raw_record[key]
                for key in ("raw_bytes", "bytes", "content", "base64")
                if key in raw_record
            }
            transport_payload["adapter"] = "fixture"
        transport = _transport_dict(transport_payload, root, stable_key)
        expected = transport["expected_raw_hash"]
        if expected and expected != source_ref.content_hash:
            raise CatalogValidationError(
                "source_ref.content_hash and expected_raw_hash differ",
                details={
                    "stable_key": stable_key,
                    "source_ref_hash": source_ref.content_hash,
                    "expected_raw_hash": expected,
                },
            )
        records.append(
            _CatalogRecord(
                stable_key=stable_key,
                source_ref=source_ref,
                transport=transport,
                ordinal=ordinal,
            )
        )
    policy_inputs = {
        "authority_mode": _string_value(
            catalog,
            "authority_mode",
            "authorityMode",
            required=False,
            default="official-first",
        ),
        "comparison_mode": _string_value(
            catalog,
            "comparison_mode",
            "comparisonMode",
            required=False,
            default=COMPARISON_POLICY_VERSION,
        ),
    }
    if policy_inputs["authority_mode"] != "official-first":
        raise CatalogValidationError(
            "only official-first authority mode is supported",
            details={"authority_mode": policy_inputs["authority_mode"]},
        )
    if policy_inputs["comparison_mode"] not in {
        COMPARISON_POLICY_VERSION,
        "exact",
        "sha256",
    }:
        raise CatalogValidationError(
            "comparison policy is not supported",
            details={"comparison_mode": policy_inputs["comparison_mode"]},
        )
    metadata = {
        "parser_version": parser_pin,
        "transform_version": transform_pin,
        "validation_policy": validation_policy,
        "authority_mode": policy_inputs["authority_mode"],
        "comparison_policy": policy_inputs["comparison_mode"],
    }
    return catalog_id, corpus_version, tuple(records), metadata


select_checkpoint = selection_keys
normalize_key = normalize_stable_key

__all__ = [
    "normalize_key",
    "normalize_stable_key",
    "select_checkpoint",
    "selection_keys",
]
