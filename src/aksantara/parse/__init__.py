"""Parse package — deterministic KBBI parsers, no LLM."""

from aksantara.parse.kbbi_html import parse_kbbi as parse_kbbi_html
from aksantara.parse.kbbi_json import parse_kbbi as parse_kbbi_json
from aksantara.parse.parser_contract import (
    PARSER_VERSION,
    BaseKBBIParser,
    KBBIParser,
    MissingFieldError,
    ParseError,
    ParserError,
    UnsupportedSourceError,
    parse_kbbi,
)

__all__ = [
    "PARSER_VERSION",
    "BaseKBBIParser",
    "KBBIParser",
    "MissingFieldError",
    "ParseError",
    "ParserError",
    "UnsupportedSourceError",
    "parse_kbbi",
    "parse_kbbi_html",
    "parse_kbbi_json",
]
