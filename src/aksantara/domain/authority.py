"""Authority layers and validation policy for Aksantara.

KBBI is the sole lexical source of truth. Every write path must declare
its AuthorityLayer; only official layers can assert or override canonical
fields. Lower layers are enrichment/evaluation only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Authority layers — ordered from highest to lowest trust.
# ---------------------------------------------------------------------------


class AuthorityLayer(StrEnum):
    """Gov-first authority ordering (highest trust first)."""

    KBBI_OFFICIAL_LIVE = "kbbi_official_live"
    KBBI_OFFICIAL_SNAPSHOT = "kbbi_official_snapshot"
    BADAN_BAHASA_RULE = "badan_bahasa_rule"
    SIPEBI_PUBLIC = "sipebi_public"
    GOV_DERIVED_SNAPSHOT = "gov_derived_snapshot"
    MIRROR_FALLBACK = "mirror_fallback"
    ENRICHMENT = "enrichment"
    EVALUATION = "evaluation"
    AI_PROPOSAL = "ai_proposal"

    @property
    def is_canonical_writer(self) -> bool:
        """True if this layer may write canonical KBBI fields."""
        return self in {
            AuthorityLayer.KBBI_OFFICIAL_LIVE,
            AuthorityLayer.KBBI_OFFICIAL_SNAPSHOT,
        }

    @property
    def is_rule_authority(self) -> bool:
        """True if this layer may assert orthographic/structural rules."""
        return self in {
            AuthorityLayer.BADAN_BAHASA_RULE,
            AuthorityLayer.SIPEBI_PUBLIC,
        }

    @property
    def is_fallback(self) -> bool:
        return self in {
            AuthorityLayer.GOV_DERIVED_SNAPSHOT,
            AuthorityLayer.MIRROR_FALLBACK,
        }

    @property
    def is_non_authoritative(self) -> bool:
        return self in {
            AuthorityLayer.ENRICHMENT,
            AuthorityLayer.EVALUATION,
            AuthorityLayer.AI_PROPOSAL,
        }


# SourceKind maps to storage-facing label; keep stable for Firestore index.
SourceKind = Literal[
    "official-live",
    "official-snapshot",
    "rule",
    "sipebi",
    "gov-derived",
    "fallback",
    "enrichment",
    "evaluation",
    "ai-proposal",
]

AUTHORITY_TO_SOURCE_KIND: dict[AuthorityLayer, SourceKind] = {
    AuthorityLayer.KBBI_OFFICIAL_LIVE: "official-live",
    AuthorityLayer.KBBI_OFFICIAL_SNAPSHOT: "official-snapshot",
    AuthorityLayer.BADAN_BAHASA_RULE: "rule",
    AuthorityLayer.SIPEBI_PUBLIC: "sipebi",
    AuthorityLayer.GOV_DERIVED_SNAPSHOT: "gov-derived",
    AuthorityLayer.MIRROR_FALLBACK: "fallback",
    AuthorityLayer.ENRICHMENT: "enrichment",
    AuthorityLayer.EVALUATION: "evaluation",
    AuthorityLayer.AI_PROPOSAL: "ai-proposal",
}

# ---------------------------------------------------------------------------
# Validation policy — deterministic, reviewable, fail-closed.
# ---------------------------------------------------------------------------


class ValidationPolicy(BaseModel):
    """Policy gates applied during VALIDATE stage.

    All fields have safe defaults; callers should use the singleton
    `DEFAULT_VALIDATION_POLICY` unless a human-approved override is in effect.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    require_official_source_for_canonical: bool = True
    allow_fallback_only_with_explicit_label: bool = True
    quarantine_on_status_conflict: bool = True
    quarantine_on_non_deterministic_replay: bool = True
    fail_closed_on_unknown_semantic: bool = True
    require_human_review_for_conflicts: bool = True
    require_human_review_for_release: bool = True
    min_confidence_for_auto_approve: float = 0.95
    allowed_statuses: tuple[str, ...] = ("active", "inactive", "unresolved")
    allowed_review_statuses: tuple[str, ...] = (
        "pending",
        "approved",
        "rejected",
        "quarantined",
    )
    blocked_enrichment_namespaces: tuple[str, ...] = ("entries", "vector_entries")

    def assert_canonical_writer(self, layer: AuthorityLayer) -> None:
        """Raise if layer is not permitted to write canonical fields."""
        if self.require_official_source_for_canonical and not layer.is_canonical_writer:
            from aksantara.domain.errors import AuthorityViolationError

            raise AuthorityViolationError(
                f"Layer {layer.value} cannot write canonical KBBI fields; "
                f"only {AuthorityLayer.KBBI_OFFICIAL_LIVE.value} and "
                f"{AuthorityLayer.KBBI_OFFICIAL_SNAPSHOT.value} may."
            )

    def is_valid_status(self, status: str) -> bool:
        return status in self.allowed_statuses

    def is_valid_review_status(self, review_status: str) -> bool:
        return review_status in self.allowed_review_statuses


DEFAULT_VALIDATION_POLICY = ValidationPolicy()

__all__ = [
    "AUTHORITY_TO_SOURCE_KIND",
    "DEFAULT_VALIDATION_POLICY",
    "AuthorityLayer",
    "SourceKind",
    "ValidationPolicy",
]
