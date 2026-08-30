"""Security invariants — enrichment cannot enter canonical, AI cannot invent."""

from datetime import UTC, datetime

import pytest

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.domain.provenance import content_hash_bytes
from aksantara.validate.schema import validate_entry


def test_enrichment_blocked() -> None:
    raw = b"enrichment"
    source = SourceRef(
        url="https://example.com/enrich",
        source_kind="enrichment",
        edition="VI",
        source_version="VI",
        retrieved_at=datetime.now(UTC),
        content_hash=content_hash_bytes(raw),
    )
    entry = KBBIEntry(
        id="enrich",
        lema="Enrich",
        makna=[{"definisi": "enriched"}],
        source=source,
    )
    from aksantara.domain.errors import AuthorityViolationError, QuarantinedError

    with pytest.raises((QuarantinedError, AuthorityViolationError)):
        validate_entry(entry)


def test_generic_corpus_cannot_override() -> None:
    # Attempt to inject AI-generated definition — must be quarantined via sourceKind
    raw = b"ai hallucination"
    source = SourceRef(
        url="https://ai.example.com",
        source_kind="ai-proposal",
        edition="VI",
        source_version="VI",
        retrieved_at=datetime.now(UTC),
        content_hash=content_hash_bytes(raw),
    )
    entry = KBBIEntry(
        id="halluc",
        lema="Halluc",
        makna=[{"definisi": "invented by LLM"}],
        source=source,
    )
    from aksantara.domain.errors import AuthorityViolationError, QuarantinedError

    with pytest.raises((QuarantinedError, AuthorityViolationError)):
        validate_entry(entry)
