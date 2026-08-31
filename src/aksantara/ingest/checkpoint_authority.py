"""Authority observation processing for checkpoint execution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksantara.ingest.checkpoint_types import _CatalogRecord


def _source_identity(source_ref: Any) -> dict[str, Any]:
    """Return the stable and volatile provenance fields for an observation."""
    payload = source_ref.model_dump(mode="json")
    return {
        "url": payload["url"],
        "source_kind": payload["source_kind"],
        "edition": payload["edition"],
        "source_version": payload["source_version"],
        "retrieved_at": payload["retrieved_at"],
        "content_hash": payload["content_hash"],
        "parser_version": payload["parser_version"],
    }


class CheckpointAuthorityMixin:
    """Process ordered official/evidence observations and review records."""

    def _observe_binding(
        self: Any,
        record: _CatalogRecord,
        *,
        binding: Mapping[str, Any],
        run_dir: Path,
        selected_index: int,
        binding_index: int,
    ) -> dict[str, Any]:
        """Read and validate one physical observation through the seam."""
        from aksantara.ingest.checkpoint_observation import observe_binding

        return observe_binding(
            self,
            record,
            binding=binding,
            run_dir=run_dir,
            selected_index=selected_index,
            binding_index=binding_index,
        )

    @staticmethod
    def _ordered_bindings(record: _CatalogRecord) -> list[dict[str, Any]]:
        """Put an adapter-verified official binding before all evidence."""
        primary = {
            "role": "official"
            if record.source_ref.source_kind in {"official-live", "official-snapshot"}
            else "evidence",
            "source_ref": record.source_ref,
            "transport": record.transport,
        }
        values = [primary, *record.observations]
        normalized: list[dict[str, Any]] = []
        for value_index, value in enumerate(values):
            source_kind = value["source_ref"].source_kind
            role = (
                "official"
                if source_kind in {"official-live", "official-snapshot"}
                else str(value.get("role", "evidence"))
            )
            normalized.append(
                {
                    **value,
                    "role": role,
                    "_primary_binding": value_index == 0,
                }
            )
        values = normalized
        values.sort(
            key=lambda value: (
                0
                if value["source_ref"].source_kind
                in {"official-live", "official-snapshot"}
                else 1,
                0 if bool(value.get("_primary_binding")) else 1,
                str(value["source_ref"].url),
                str(value["source_ref"].content_hash),
            )
        )
        return values

    @staticmethod
    def _current_run_id(run_dir: Path) -> str:
        return run_dir.name
