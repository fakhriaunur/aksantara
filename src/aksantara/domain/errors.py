"""Domain exceptions for Aksantara.

All lexical invariants produce explicit, typed errors. Callers must not
swallow these into generic exceptions; escalation policy depends on the
concrete type.

- QuarantinedError: source conflict, authority violation, or parser
  anomaly that must not enter the canonical corpus.
- NonDeterministicError: deterministic replay invariant broken; same raw
  input produced divergent canonical output.
"""

from __future__ import annotations


class AksantaraDomainError(Exception):
    """Base for all domain errors. Never raised directly."""

    pass


class QuarantinedError(AksantaraDomainError):
    """Entry or source quarantined; blocked from canonical/vector stores.

    Attributes:
        reason: machine-readable quarantine code.
        entry_id: lema or id that triggered quarantine, if known.
        source_kind: authority layer that supplied the record.
        details: optional human-readable context for review queue.
    """

    def __init__(
        self,
        reason: str,
        *,
        entry_id: str | None = None,
        source_kind: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(reason if details is None else f"{reason}: {details}")
        self.reason = reason
        self.entry_id = entry_id
        self.source_kind = source_kind
        self.details = details


class NonDeterministicError(AksantaraDomainError):
    """Deterministic replay failed: same contentHash yielded different output.

    Attributes:
        content_hash: sha256 of the raw snapshot that was replayed.
        parser_version: parser that produced divergent output.
        expected: optional hash or JSON digest of expected canonical form.
        actual: optional hash or JSON digest of actual canonical form.
    """

    def __init__(
        self,
        message: str,
        *,
        content_hash: str | None = None,
        parser_version: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        super().__init__(message)
        self.content_hash = content_hash
        self.parser_version = parser_version
        self.expected = expected
        self.actual = actual


class AuthorityViolationError(AksantaraDomainError):
    """Attempt to assert lexical fact from a non-authoritative layer."""

    pass


class ValidationError(AksantaraDomainError):
    """Canonical invariant violation during validate_entry."""

    pass


__all__ = [
    "AksantaraDomainError",
    "AuthorityViolationError",
    "NonDeterministicError",
    "QuarantinedError",
    "ValidationError",
]
