"""Catalog record assembly for the deterministic checkpoint driver."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksantara.ingest.checkpoint_catalog import (
    _additional_observations,
    _parse_source_ref,
    _string_value,
    _transport_dict,
    normalize_stable_key,
)
from aksantara.ingest.checkpoint_types import (
    _CONTROL_RE,
    AUTHORITY_POLICY_VERSION,
    COMPARISON_POLICY_VERSION,
    TRANSFORM_VERSION,
    CatalogValidationError,
    _CatalogRecord,
)
from aksantara.parse.parser_contract import PARSER_VERSION


def catalog_records(
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
                observations=_additional_observations(
                    raw_record,
                    root=root,
                    stable_key=stable_key,
                ),
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
