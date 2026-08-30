"""Parser contract for Aksantara KBBI — abstract interface.

Deterministic, no LLM, no I/O. Concrete parsers (kbbi_html, kbbi_json)
implement `parse_kbbi(raw_bytes, source_ref) -> KBBIEntry`.

Parser version is pinned per build; mismatch quarantines.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from aksantara.domain.errors import AksantaraDomainError
from aksantara.domain.models import KBBIEntry, SourceRef

PARSER_VERSION: str = "0.1.0"


class ParserError(AksantaraDomainError):
    """Base for parser errors. Never raised directly."""

    pass


class ParseError(ParserError):
    """Generic parse failure — malformed input or missing required field."""

    pass


class MissingFieldError(ParseError):
    """Required field absent in source (e.g., lema or makna)."""

    pass


class UnsupportedSourceError(ParserError):
    """Source kind or content-type not supported by this parser."""

    pass


@runtime_checkable
class KBBIParser(Protocol):
    """Protocol for KBBI parsers."""

    parser_version: str

    def parse(self, raw_bytes: bytes, source_ref: SourceRef) -> KBBIEntry: ...


class BaseKBBIParser(ABC):
    """Abstract base class for parsers. Concrete parsers should subclass."""

    parser_version: str = PARSER_VERSION

    @abstractmethod
    def parse(self, raw_bytes: bytes, source_ref: SourceRef) -> KBBIEntry:
        raise NotImplementedError


def parse_kbbi(raw_bytes: bytes, source_ref: SourceRef) -> KBBIEntry:
    """Contract dispatcher — deterministic, no LLM.

    Auto-detects JSON vs HTML and delegates to concrete parser.
    Concrete modules (kbbi_html, kbbi_json) implement same signature;
    this dispatcher satisfies `tests/replay/test_replay.py` which imports
    from parser_contract directly.

    Args:
        raw_bytes: immutable snapshot bytes (HTML or JSON).
        source_ref: provenance pointer.

    Returns:
        KBBIEntry canonical aggregate.

    Raises:
        ParserError: on malformed input.
    """
    if not raw_bytes:
        raise ParseError("raw_bytes is empty")
    stripped: bytes = raw_bytes.lstrip()
    # Heuristic: JSON payload
    is_json: bool = stripped.startswith(b"{") or stripped.startswith(b"[")
    if is_json:
        try:
            # Validate JSON decodable before delegating
            import json as _json

            _json.loads(stripped.decode("utf-8"))
            from aksantara.parse.kbbi_json import parse_kbbi as json_parse

            return json_parse(raw_bytes, source_ref)
        except Exception:
            # Fall through to HTML if JSON parse fails
            pass
    # Default to HTML parser (also handles fallback mirrors)
    try:
        from aksantara.parse.kbbi_html import parse_kbbi as html_parse

        return html_parse(raw_bytes, source_ref)
    except Exception as exc:
        # If HTML also fails and raw was JSON-like, try JSON again to surface error
        if is_json:
            from aksantara.parse.kbbi_json import parse_kbbi as json_parse2

            return json_parse2(raw_bytes, source_ref)
        raise exc


# Attach version attribute for introspection (mirrors module constant)
parse_kbbi.parser_version = PARSER_VERSION  # type: ignore[attr-defined]

__all__ = [
    "PARSER_VERSION",
    "BaseKBBIParser",
    "KBBIParser",
    "MissingFieldError",
    "ParseError",
    "ParserError",
    "UnsupportedSourceError",
    "parse_kbbi",
]
