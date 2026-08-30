"""Deterministic replay gate — verifies same raw + sourceRef -> same KBBIEntry.

Uses canonical_json_hash to compare expected vs actual canonical JSON.
Raises NonDeterministicError if diverges. Pure function, no I/O beyond parsing.

Supports both legacy spec signature replay_raw(raw_bytes, sourceRef, expected_entry)
and test suite signature replay_raw(raw_bytes, source, expected=...).
"""

from __future__ import annotations

from collections.abc import Callable

from aksantara.domain.errors import NonDeterministicError
from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.domain.provenance import canonical_json_hash
from aksantara.parse.parser_contract import parse_kbbi


def replay_raw(
    raw_bytes: bytes,
    source: SourceRef,
    expected: KBBIEntry | None = None,
    expected_entry: KBBIEntry | None = None,
    *,
    parser: Callable[[bytes, SourceRef], KBBIEntry] | None = None,
    source_ref: SourceRef | None = None,
    sourceRef: SourceRef | None = None,
) -> KBBIEntry:
    """Re-parse raw and verify determinism against expected.

    Args:
        raw_bytes: immutable snapshot bytes (HTML or JSON).
        source: provenance (also accepts source_ref / sourceRef aliases).
        expected: canonical entry previously produced (keyword as used in tests).
        expected_entry: alias for expected (as per task spec).
        parser: optional explicit parser function; defaults to parser_contract dispatcher.
        source_ref / sourceRef: alias for source param.

    Returns:
        Parsed entry if deterministic (or first pass if expected is None).

    Raises:
        NonDeterministicError if hashes diverge.
        ValueError if raw_bytes empty.
    """
    # Handle aliasing for source param
    effective_source: SourceRef = source
    if source_ref is not None:
        effective_source = source_ref
    if sourceRef is not None:
        effective_source = sourceRef
    # Handle aliasing for expected
    effective_expected: KBBIEntry | None = (
        expected if expected is not None else expected_entry
    )
    if not raw_bytes:
        raise ValueError("raw_bytes must be non-empty")
    parse_fn: Callable[[bytes, SourceRef], KBBIEntry] = parser or parse_kbbi
    parsed: KBBIEntry = parse_fn(raw_bytes, effective_source)
    if effective_expected is None:
        return parsed
    exp_hash: str = canonical_json_hash(effective_expected.model_dump(mode="json"))  # type: ignore
    act_hash: str = canonical_json_hash(parsed.model_dump(mode="json"))  # type: ignore
    if exp_hash != act_hash:
        raise NonDeterministicError(
            f"replay divergence for {effective_source.url}: hashes differ",
            content_hash=effective_source.content_hash,
            parser_version=effective_source.parser_version,
            expected=exp_hash,
            actual=act_hash,
        )
    # Also ensure parser_version matches
    if parsed.parser_version != effective_expected.parser_version:
        raise NonDeterministicError(
            f"parser_version divergence: expected {effective_expected.parser_version} actual {parsed.parser_version}",
            content_hash=effective_source.content_hash,
            parser_version=parsed.parser_version,
            expected=effective_expected.parser_version,
            actual=parsed.parser_version,
        )
    return parsed


def assert_deterministic(raw_bytes: bytes, source: SourceRef) -> None:
    """Run parse twice and assert same canonical hash.

    Raises NonDeterministicError if diverges.
    """
    first: KBBIEntry = parse_kbbi(raw_bytes, source)
    second: KBBIEntry = parse_kbbi(raw_bytes, source)
    h1: str = canonical_json_hash(first.model_dump(mode="json"))  # type: ignore
    h2: str = canonical_json_hash(second.model_dump(mode="json"))  # type: ignore
    if h1 != h2:
        raise NonDeterministicError(
            "non-deterministic parser: two parses differ",
            content_hash=source.content_hash,
            parser_version=source.parser_version,
            expected=h1,
            actual=h2,
        )


def verify_replay(
    raw_bytes: bytes,
    source: SourceRef,
    expected: KBBIEntry,
    *,
    parser: Callable[[bytes, SourceRef], KBBIEntry] | None = None,
) -> bool:
    """Return True if replay is deterministic, else False (does not raise)."""
    try:
        replay_raw(raw_bytes, source, expected=expected, parser=parser)
        return True
    except NonDeterministicError:
        return False


# Compatibility aliases for task spec naming
replay = replay_raw

__all__ = ["assert_deterministic", "replay_raw", "verify_replay"]
