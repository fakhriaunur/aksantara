"""Deterministic KBBI Daring HTML parser — bs4+lxml, no LLM.

Selectors (in order):
  lema: .lema, .kata, h2.lema, h1, h2, title
  makna: .makna li, .definisi li, ol li, ul li
  contoh: .contoh, then i/em inside .makna li as fallback
  bentukTidakBaku: .bentukTidakBaku, .bentuk-tidak-baku, .tidak-baku, .tidakBaku, .varian plus text search "bentuk tidak baku:"

Handles Pebruari→Februari via bentuk_tidak_baku list (no inference).

Determinism: fixed selector order, preserved source order, no random, no LLM.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.parse.parser_contract import (
    PARSER_VERSION,
    MissingFieldError,
    ParseError,
)

# Re-export version for callers
__all__ = ["PARSER_VERSION", "parse_kbbi", "parse_kbbi_html"]


def _clean_text(text: str) -> str:
    # Collapse whitespace, strip zero-width, normalize
    if not text:
        return ""
    # remove zero-width chars
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key: str = it.strip()
        if not key:
            continue
        # case-sensitive dedup; but also avoid case-insensitive dup for variants?
        # Keep case-sensitive to preserve Pebruari vs pebruari distinction.
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _extract_lema(soup: BeautifulSoup, source_ref: SourceRef) -> str:
    selectors: list[str] = [".lema", ".kata", "h2.lema", "h1", "h2", ".word", "title"]
    for sel in selectors:
        el = soup.select_one(sel)
        if el is not None:
            txt: str = _clean_text(el.get_text(" ", strip=True))
            if txt:
                # title may contain "Februari - KBBI ..." split
                if sel == "title" and " - " in txt:
                    txt = txt.split(" - ")[0]
                # remove trailing superscript numbers like "Februari 1"
                txt = re.sub(r"\s+\d+$", "", txt)
                if txt:
                    return txt
    # Fallback: infer from source_ref URL last segment
    url: str = source_ref.url.rstrip("/")
    if "/" in url:
        last: str = url.split("/")[-1]
        # url decode basic
        try:
            from urllib.parse import unquote

            last = unquote(last)
        except Exception:
            pass
        last = _clean_text(last)
        if last:
            # Capitalize first letter to match KBBI style if slug is lowercase
            # But preserve as-is if already plausible word
            return last[:1].upper() + last[1:] if last.islower() else last
    raise MissingFieldError(f"lema not found in HTML for {source_ref.url}")


def _extract_kelas_kata(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    # selector based
    for sel in [
        ".kelas-kata",
        ".kelaskata",
        ".kelas",
        ".pos",
        "span.kelas",
        "em.pos",
        ".word-class",
    ]:
        for el in soup.select(sel):
            txt: str = _clean_text(el.get_text(" ", strip=True))
            if not txt:
                continue
            # may contain multiple like "n, v" or "n"
            for part in re.split(r"[,;/\s]+", txt):
                p: str = _clean_text(part).lower()
                if p and p in {
                    "n",
                    "v",
                    "a",
                    "adv",
                    "pron",
                    "num",
                    "p",
                    "interj",
                    "konj",
                    "prep",
                    "adj",
                    "adv.",
                    "n.",
                    "v.",
                    "num.",
                }:
                    # normalize without dot
                    p = p.strip(".")
                    out.append(p)
                elif p and len(p) <= 4 and p.isalpha():
                    # keep as-is for other POS tags
                    out.append(p)
    # fallback: search text for pattern "(n)" near lema
    if not out:
        text: str = soup.get_text(" ", strip=True)
        # look for word class in parentheses after lema, e.g., "Februari (n)"
        m = re.search(
            r"\(\s*(n|v|a|adv|pron|num|p|interj|konj|prep|adj)\s*\)",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            out.append(m.group(1).lower())
    # deduplicate preserving order, lowercased distinct
    dedup: list[str] = []
    seen: set[str] = set()
    for x in out:
        xl: str = x.lower().strip(".")
        if xl and xl not in seen:
            seen.add(xl)
            dedup.append(xl)
    return dedup


def _extract_makna(soup: BeautifulSoup) -> list[dict[str, Any]]:
    containers: list[Any] = []
    # try selectors in order
    for sel in [".makna li", ".definisi li", ".arti li"]:
        els = soup.select(sel)
        if els:
            containers = els
            break
    if not containers:
        # look for ol li within .makna container or global ol
        # prefer .makna ol li
        els = soup.select(".makna ol li")
        if els:
            containers = els
        else:
            els = soup.select("ol li")
            # filter to avoid nav/footer: require li length > 5 and not inside header/nav
            filtered: list[Any] = []
            for li in els:
                # skip if inside nav/header/footer
                parent_names: list[str] = [
                    p.name for p in li.parents if getattr(p, "name", None)
                ]
                if any(n in {"nav", "header", "footer"} for n in parent_names):
                    continue
                txt: str = _clean_text(li.get_text(" ", strip=True))
                if len(txt) >= 5:
                    filtered.append(li)
            if filtered:
                containers = filtered
    if not containers:
        els = soup.select("ul li")
        filtered2: list[Any] = []
        for li in els:
            parent_names = [p.name for p in li.parents if getattr(p, "name", None)]
            if any(n in {"nav", "header", "footer"} for n in parent_names):
                continue
            txt = _clean_text(li.get_text(" ", strip=True))
            if len(txt) >= 5:
                filtered2.append(li)
        if filtered2:
            containers = filtered2

    makna: list[dict[str, Any]] = []
    for li in containers:
        # Extract text, but exclude contoh i tags to avoid duplicating example in definition?
        # For determinism, take full li text then later strip example prefix if needed
        # If li contains <i> that is example, we keep definition part before <i>
        # Clone: get text up to first <i>
        # Simple: get full text, then remove contoh that will be extracted separately
        raw_txt: str = _clean_text(li.get_text(" ", strip=True))
        if not raw_txt:
            continue
        # Remove leading numbering "1. " if present
        raw_txt = re.sub(r"^\s*\d+[\.\)]\s*", "", raw_txt)
        # If raw_txt contains "contoh:" split and keep definition part
        # Example: "bulan kedua ... contoh: Ia lahir ..."
        # We'll split on "contoh:"
        if re.search(r"contoh\s*:", raw_txt, flags=re.IGNORECASE):
            raw_txt = re.split(r"contoh\s*:", raw_txt, flags=re.IGNORECASE)[0].strip()
            raw_txt = _clean_text(raw_txt)
        if not raw_txt:
            continue
        makna.append({"definisi": raw_txt})

    # Fallback: if no li found, try to find .makna div text with numbered definitions
    if not makna:
        makna_div = (
            soup.select_one(".makna")
            or soup.select_one(".definisi")
            or soup.select_one(".arti")
        )
        if makna_div is not None:
            div_text: str = _clean_text(makna_div.get_text(" ", strip=True))
            # split by numbered pattern like "1. " "2. "
            parts: list[str] = re.split(r"\s*\d+[\.\)]\s+", div_text)
            for part in parts:
                part = _clean_text(part)
                if part and len(part) >= 5:
                    # remove contoh tail
                    if re.search(r"contoh\s*:", part, flags=re.IGNORECASE):
                        part = re.split(r"contoh\s*:", part, flags=re.IGNORECASE)[
                            0
                        ].strip()
                    if part:
                        makna.append({"definisi": part})
        # else try generic paragraph
        if not makna:
            # try first <p> with substantial text
            for p in soup.find_all("p"):
                txt = _clean_text(p.get_text(" ", strip=True))
                if len(txt) >= 10:
                    makna.append({"definisi": txt})
                    break

    return makna


def _extract_contoh(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    # Primary: .contoh
    for el in soup.select(".contoh"):
        txt: str = _clean_text(el.get_text(" ", strip=True))
        if not txt:
            continue
        # strip leading "contoh:" label
        txt = re.sub(r"^contoh\s*:\s*", "", txt, flags=re.IGNORECASE)
        txt = _clean_text(txt)
        if txt:
            out.append(txt)
    # Fallback inside makna li <i> tags if no .contoh found
    if not out:
        for li in soup.select(".makna li"):
            for i_tag in li.find_all("i"):
                txt = _clean_text(i_tag.get_text(" ", strip=True))
                if not txt:
                    continue
                txt = re.sub(r"^contoh\s*:\s*", "", txt, flags=re.IGNORECASE)
                if txt and len(txt) >= 5:
                    out.append(txt)
            for em in li.find_all("em"):
                txt = _clean_text(em.get_text(" ", strip=True))
                if txt and len(txt) >= 8 and txt not in out:
                    # heuristic: examples are sentences, not single words
                    if " " in txt:
                        out.append(txt)
    # Also check general <i> that look like sentences (heuristic)
    # Do not over-collect; limit to avoid nav noise
    deduped: list[str] = _dedup_preserve_order(out)
    return deduped


def _extract_bentuk_tidak_baku(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    selectors: list[str] = [
        ".bentukTidakBaku",
        ".bentuk-tidak-baku",
        ".bentuk_tidak_baku",
        ".tidak-baku",
        ".tidakBaku",
        ".varian",
        ".nonstandard",
        ".bentukTidakBakuSpan",
    ]
    for sel in selectors:
        for el in soup.select(sel):
            txt: str = _clean_text(el.get_text(" ", strip=True))
            if not txt:
                continue
            # extract after colon if present
            if ":" in txt:
                txt = txt.split(":", 1)[1]
            txt = _clean_text(txt)
            # split by comma/semicolon
            for part in re.split(r"[,;]", txt):
                part = _clean_text(part)
                if part:
                    # remove "bentuk tidak baku" prefix if still there
                    part = re.sub(
                        r"^bentuk\s+tidak\s+baku\s*", "", part, flags=re.IGNORECASE
                    ).strip()
                    if part:
                        out.append(part)
    # Text search fallback: any string containing "bentuk tidak baku"
    pattern: re.Pattern[str] = re.compile(
        r"bentuk\s+tidak\s+baku\s*[:\-]\s*([A-Za-z0-9,\s\-]+)", re.IGNORECASE
    )
    for text_node in soup.find_all(string=pattern):
        m: re.Match[str] | None = pattern.search(str(text_node))
        if m:
            raw: str = m.group(1)
            # truncate at period or newline
            raw = raw.split(".")[0]
            raw = raw.split("\n")[0]
            for part in re.split(r"[,;]", raw):
                part = _clean_text(part)
                if part:
                    out.append(part)
    # Also consider pattern without colon but after phrase
    # e.g., "bentuk tidak baku Pebruari"
    pattern2: re.Pattern[str] = re.compile(
        r"bentuk\s+tidak\s+baku\s+([A-Za-z]+)", re.IGNORECASE
    )
    for text_node in soup.find_all(string=pattern2):
        m = pattern2.search(str(text_node))
        if m:
            out.append(_clean_text(m.group(1)))

    # Clean and dedup preserving order
    cleaned: list[str] = []
    for x in out:
        x = _clean_text(x)
        # Remove trailing punctuation
        x = re.sub(r"[.;:]+$", "", x).strip()
        if x:
            cleaned.append(x)
    return _dedup_preserve_order(cleaned)


def parse_kbbi(raw_bytes: bytes, source_ref: SourceRef) -> KBBIEntry:
    """Parse KBBI Daring HTML bytes into KBBIEntry deterministically.

    Args:
        raw_bytes: HTML snapshot bytes (utf-8).
        source_ref: provenance (contentHash must match raw_bytes for validation, but not enforced here).

    Returns:
        KBBIEntry with full provenance.

    Raises:
        ParseError / MissingFieldError on malformed or missing required fields.
    """
    if not raw_bytes:
        raise ParseError("raw_bytes is empty")
    try:
        text: str = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"raw_bytes not valid utf-8: {exc}") from exc

    # Deterministic parse with lxml
    soup: BeautifulSoup = BeautifulSoup(text, "lxml")

    lema: str = _extract_lema(soup, source_ref)
    if not lema:
        raise MissingFieldError("lema extraction returned empty")

    makna: list[dict[str, Any]] = _extract_makna(soup)
    if not makna:
        raise MissingFieldError(
            f"makna not found for lema {lema!r} at {source_ref.url}"
        )

    kelas_kata: list[str] = _extract_kelas_kata(soup)
    contoh: list[str] = _extract_contoh(soup)
    bentuk_tidak_baku: list[str] = _extract_bentuk_tidak_baku(soup)

    # Build deterministic id: lowercased lema slug, replace spaces with hyphen? For now simple lower
    entry_id: str = lema.strip().lower()
    # sanitize id: keep alphanum, hyphen, underscore; replace spaces with hyphen
    entry_id = re.sub(r"\s+", "-", entry_id)
    entry_id = re.sub(r"[^a-z0-9\-_]", "", entry_id)
    if not entry_id:
        entry_id = lema.strip().lower()

    # Ensure makna each dict has definisi; validator will enforce
    # Deterministically ensure order is source order (already)

    try:
        entry: KBBIEntry = KBBIEntry(
            id=entry_id,
            lema=lema,
            makna=makna,
            kelas_kata=kelas_kata,
            contoh=contoh,
            bentuk_tidak_baku=bentuk_tidak_baku,
            bentuk_baku=None,
            status="active",
            source=source_ref,
            parser_version=PARSER_VERSION,
            transform_version="0.1.0",
            review_status="pending",
            confidence=1.0,
        )
    except Exception as exc:
        raise ParseError(f"failed to build KBBIEntry for {lema!r}: {exc}") from exc

    return entry


# Alias for explicit import
parse_kbbi_html = parse_kbbi
