"""Domain package — KBBI canonical models, authority, provenance, errors."""

from aksantara.domain.authority import (
    AUTHORITY_TO_SOURCE_KIND,
    DEFAULT_VALIDATION_POLICY,
    AuthorityLayer,
    SourceKind,
    ValidationPolicy,
)
from aksantara.domain.errors import (
    AksantaraDomainError,
    AuthorityViolationError,
    NonDeterministicError,
    QuarantinedError,
    ValidationError,
)
from aksantara.domain.models import KBBIEntry, ReviewStatus, SourceRef, Status
from aksantara.domain.provenance import (
    CANONICAL_CONTENT_FIELDS,
    canonical_content_hash,
    canonical_content_payload,
    canonical_json_hash,
    content_hash,
    content_hash_bytes,
    verify_content_hash,
)

__all__ = [
    "AUTHORITY_TO_SOURCE_KIND",
    "CANONICAL_CONTENT_FIELDS",
    "DEFAULT_VALIDATION_POLICY",
    "AksantaraDomainError",
    "AuthorityLayer",
    "AuthorityViolationError",
    "KBBIEntry",
    "NonDeterministicError",
    "QuarantinedError",
    "ReviewStatus",
    "SourceKind",
    "SourceRef",
    "Status",
    "ValidationError",
    "ValidationPolicy",
    "canonical_content_hash",
    "canonical_content_payload",
    "canonical_json_hash",
    "content_hash",
    "content_hash_bytes",
    "verify_content_hash",
]
