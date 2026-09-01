"""Cost/request accounting for incremental embeddings."""

from __future__ import annotations

from dataclasses import dataclass

from aksantara.embeddings.metadata import COST_ESTIMATE_VERSION

__all__ = ["CostReport", "compute_cost"]


@dataclass(frozen=True, slots=True)
class CostReport:
    mode: str  # local, cloud
    estimate_version: str
    provider_calls: int
    retries: int
    reused: int
    writes: int
    chunks: int
    exclusions: int
    request_units: int
    formula: str

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "estimate_version": self.estimate_version,
            "provider_calls": self.provider_calls,
            "retries": self.retries,
            "reused": self.reused,
            "writes": self.writes,
            "chunks": self.chunks,
            "exclusions": self.exclusions,
            "request_units": self.request_units,
            "formula": self.formula,
        }


def compute_cost(
    provider_calls: int,
    retries: int,
    reused: int,
    writes: int,
    chunks: int,
    exclusions: int,
    mode: str = "local",
) -> CostReport:
    """Bounded reproducible request-unit cost.

    Formula v1: request_units = provider_calls * 1 + retries * 0
    (retries are reported separately, cost is bounded at provider_calls).
    This matches the spec: cost is bounded and reproducible from provider_calls.
    """
    request_units = provider_calls  # 1 unit per provider call, bounded
    formula = "request_units = provider_calls * 1 (retries excluded, bounded)"
    return CostReport(
        mode=mode,
        estimate_version=COST_ESTIMATE_VERSION,
        provider_calls=provider_calls,
        retries=retries,
        reused=reused,
        writes=writes,
        chunks=chunks,
        exclusions=exclusions,
        request_units=request_units,
        formula=formula,
    )
