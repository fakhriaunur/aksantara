"""Embedding document builder for Aksantara.

Builds a compact, deterministic text representation from a canonical
``KBBIEntry`` for Vertex AI embedding. No raw HTML is included.
Format per spec section 11: Lema / Ejaan / KelasKata / Makna / Contoh /
BentukTidakBaku. Deterministic ordering ensures identical input yields
identical document hash and embedding.

Pure function: no I/O, no LLM calls.
"""

from __future__ import annotations

import re

from aksantara.domain.models import KBBIEntry

__all__ = ["build_embedding_document", "build_query_document"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove any HTML tags that may have leaked into lexical fields."""
    return _HTML_TAG_RE.sub("", text)


def _normalize(text: str) -> str:
    """Collapse whitespace, strip, remove HTML, keep deterministic."""
    no_html = _strip_html(text)
    collapsed = _WHITESPACE_RE.sub(" ", no_html).strip()
    return collapsed


def _sense_text(sense: dict[str, object]) -> str:
    """Extract human-readable text from a sense dict."""
    for key in ("definisi", "makna", "arti", "sense", "definition"):
        if key in sense:
            val = sense[key]
            if isinstance(val, str):
                cleaned = _normalize(val)
                if cleaned:
                    return cleaned
            return _normalize(str(val))
    items = sorted(sense.items())
    return _normalize("; ".join(f"{k}: {v}" for k, v in items))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_embedding_document(entry: KBBIEntry) -> str:
    """Build compact embedding document for a ``KBBIEntry``.

    Deterministic, no raw HTML. Lines emitted in fixed order; absent
    optional fields omitted. Suitable for ``RETRIEVAL_DOCUMENT`` with
    ``gemini-embedding-001``.

    Format::

        Lema: Februari
        Ejaan: fe.bru.a.ri
        Kelas Kata: n
        Makna: (1) bulan kedua tahun Masehi ...; (2) ...
        Contoh: contoh kalimat 1 | contoh kalimat 2
        Bentuk Tidak Baku: Pebruari, Pebroari

    Args:
        entry: validated canonical entry.

    Returns:
        Compact text document with one field per line.
    """
    lines: list[str] = []

    lema = _normalize(entry.lema)
    if lema:
        lines.append(f"Lema: {lema}")

    if entry.ejaan is not None:
        ejaan = _normalize(entry.ejaan)
        if ejaan:
            lines.append(f"Ejaan: {ejaan}")

    if entry.kelas_kata:
        cleaned = [_normalize(k) for k in entry.kelas_kata if _normalize(k)]
        if cleaned:
            lines.append(f"Kelas Kata: {', '.join(cleaned)}")

    if entry.makna:
        senses: list[str] = []
        for idx, sense in enumerate(entry.makna, start=1):
            txt = _sense_text(sense)  # type: ignore[arg-type]
            if txt:
                senses.append(f"({idx}) {txt}")
        if senses:
            lines.append(f"Makna: {'; '.join(senses)}")

    if entry.contoh:
        examples = [_normalize(e) for e in entry.contoh if _normalize(e)]
        if examples:
            lines.append(f"Contoh: {' | '.join(examples)}")

    if entry.bentuk_tidak_baku:
        variants = [_normalize(v) for v in entry.bentuk_tidak_baku if _normalize(v)]
        if variants:
            lines.append(f"Bentuk Tidak Baku: {', '.join(variants)}")

    # Optional enrichment — only when present in source, never invented.
    if entry.pelafalan is not None:
        pel = _normalize(entry.pelafalan)
        if pel:
            lines.append(f"Pelafalan: {pel}")

    if entry.etimologi is not None:
        etim = _normalize(entry.etimologi)
        if etim:
            lines.append(f"Etimologi: {etim}")

    if entry.labels:
        labs = [_normalize(lb) for lb in entry.labels if _normalize(lb)]
        if labs:
            lines.append(f"Label: {', '.join(labs)}")

    return "\n".join(lines)


def build_query_document(query: str) -> str:
    """Normalize a user query for RETRIEVAL_QUERY embedding."""
    return _normalize(query)
