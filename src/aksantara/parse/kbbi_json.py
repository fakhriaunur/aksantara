"""Deterministic KBBI JSON parser — fallback JSON, same contract as HTML.

Supports camelCase and snake_case keys. No LLM, deterministic ordering.
Handles Pebruari→Februari via bentuk_tidak_baku field.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aksantara.domain.models import KBBIEntry, SourceRef
from aksantara.parse.parser_contract import (
    PARSER_VERSION,
    MissingFieldError,
    ParseError,
)

__all__ = ["PARSER_VERSION", "parse_kbbi", "parse_kbbi_json"]


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k: str = it.strip()
        if not k:
            continue
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _get_first(payload: dict[str, Any], keys: list[str]) -> Any | None:
    # case-sensitive first, then case-insensitive fallback, deterministic order given
    for k in keys:
        if k in payload:
            return payload[k]
    # lower-case map
    lower_map: dict[str, Any] = {kk.lower(): vv for kk, vv in payload.items()}
    for k in keys:
        lk: str = k.lower()
        if lk in lower_map:
            return lower_map[lk]
    return None


def _normalize_makna(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    # If string, single sense
    if isinstance(raw, str):
        txt: str = _clean_text(raw)
        if not txt:
            return []
        return [{"definisi": txt}]
    # If dict, wrap
    if isinstance(raw, dict):
        # dict may have single sense with definisi
        # try to extract definisi-like keys
        for cand in ["definisi", "makna", "arti", "sense", "definition", "description"]:
            if cand in raw and isinstance(raw[cand], str):
                txt = _clean_text(str(raw[cand]))
                if txt:
                    return [{"definisi": txt}]
        # if dict has makna list, recurse?
        for cand in ["makna", "meanings", "definitions", "arti", "senses"]:
            if cand in raw:
                return _normalize_makna(raw[cand])
        # otherwise treat dict as sense mapping
        # if dict has definisi-like value
        return []
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                txt = _clean_text(item)
                if txt:
                    out.append({"definisi": txt})
            elif isinstance(item, dict):
                # find definisi-like key
                found: str | None = None
                for cand in [
                    "definisi",
                    "makna",
                    "arti",
                    "sense",
                    "definition",
                    "description",
                    "text",
                ]:
                    if cand in item and isinstance(item[cand], str):
                        found = _clean_text(str(item[cand]))
                        break
                    # lower case fallback
                    for kk, vv in item.items():
                        if kk.lower() in {
                            "definisi",
                            "makna",
                            "arti",
                            "sense",
                            "definition",
                        } and isinstance(vv, str):
                            found = _clean_text(str(vv))
                            break
                    if found:
                        break
                if found:
                    out.append({"definisi": found})
                else:
                    # try to handle nested
                    # if dict has single string value
                    for vv in item.values():
                        if isinstance(vv, str) and vv.strip():
                            out.append({"definisi": _clean_text(vv)})
                            break
            else:
                # ignore other types
                continue
        return out
    return []


def _normalize_contoh(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        txt: str = _clean_text(raw)
        return [txt] if txt else []
    if isinstance(raw, list):
        out: list[str] = []
        for it in raw:
            if isinstance(it, str):
                txt = _clean_text(it)
                if txt:
                    txt = re.sub(r"^contoh\s*:\s*", "", txt, flags=re.IGNORECASE)
                    out.append(txt)
            elif isinstance(it, dict):
                # try to find example text
                for cand in ["contoh", "kalimat", "example", "sentence", "text"]:
                    if cand in it and isinstance(it[cand], str):
                        txt = _clean_text(str(it[cand]))
                        if txt:
                            out.append(txt)
                        break
        return _dedup_preserve_order(out)
    if isinstance(raw, dict):
        for cand in ["contoh", "examples", "kalimat"]:
            if cand in raw:
                return _normalize_contoh(raw[cand])
    return []


def _normalize_bentuk_tidak_baku(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        txt: str = _clean_text(raw)
        if not txt:
            return []
        # split by comma/semicolon
        parts: list[str] = []
        for p in re.split(r"[,;]", txt):
            p = _clean_text(p)
            if p:
                p = re.sub(r"[.;:]+$", "", p).strip()
                if p:
                    parts.append(p)
        return _dedup_preserve_order(parts)
    if isinstance(raw, list):
        out: list[str] = []
        for it in raw:
            if isinstance(it, str):
                txt = _clean_text(it)
                if txt:
                    txt = re.sub(r"[.;:]+$", "", txt).strip()
                    if txt:
                        out.append(txt)
            elif isinstance(it, dict):
                for cand in ["bentukTidakBaku", "tidakBaku", "varian", "kata", "lema"]:
                    if cand in it and isinstance(it[cand], str):
                        txt = _clean_text(str(it[cand]))
                        if txt:
                            out.append(txt)
                        break
        return _dedup_preserve_order(out)
    if isinstance(raw, dict):
        for cand in ["bentukTidakBaku", "bentuk_tidak_baku", "tidakBaku", "varian"]:
            if cand in raw:
                return _normalize_bentuk_tidak_baku(raw[cand])
    return []


def _normalize_kelas_kata(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        txt: str = _clean_text(raw).lower()
        parts: list[str] = []
        for p in re.split(r"[,;/\s]+", txt):
            p = p.strip(" .")
            if p:
                parts.append(p)
        return _dedup_preserve_order(parts)
    if isinstance(raw, list):
        out: list[str] = []
        for it in raw:
            if isinstance(it, str):
                txt = _clean_text(it).lower().strip(" .")
                if txt:
                    out.append(txt)
        return _dedup_preserve_order(out)
    return []


def parse_kbbi(raw_bytes: bytes, source_ref: SourceRef) -> KBBIEntry:
    """Parse fallback JSON bytes into KBBIEntry deterministically.

    Args:
        raw_bytes: JSON snapshot bytes (utf-8).
        source_ref: provenance.

    Returns:
        KBBIEntry.

    Raises:
        ParseError / MissingFieldError.
    """
    if not raw_bytes:
        raise ParseError("raw_bytes is empty")
    try:
        text: str = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"raw_bytes not utf-8: {exc}") from exc
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON: {exc}") from exc

    # If payload is list, find entry matching lema from source_ref url or take first
    if isinstance(payload, list):
        if not payload:
            raise MissingFieldError("JSON payload is empty list")
        # try to find matching lema slug
        url_last: str = source_ref.url.rstrip("/").split("/")[-1].lower()
        chosen: Any | None = None
        for item in payload:
            if isinstance(item, dict):
                lema_cand: Any = _get_first(
                    item, ["lema", "kata", "headword", "word", "lemma", "entry"]
                )
                if (
                    isinstance(lema_cand, str)
                    and lema_cand.strip().lower() == url_last.lower()
                ):
                    chosen = item
                    break
        if chosen is None:
            # take first dict
            for item in payload:
                if isinstance(item, dict):
                    chosen = item
                    break
        if chosen is None:
            raise MissingFieldError("JSON list contains no dict entry")
        payload = chosen

    if not isinstance(payload, dict):
        raise ParseError(f"JSON payload must be object, got {type(payload).__name__}")

    # Extract fields deterministically
    lema_raw: Any = _get_first(
        payload, ["lema", "kata", "headword", "word", "lemma", "entry", "title"]
    )
    lema: str = ""
    if isinstance(lema_raw, str):
        lema = _clean_text(lema_raw)
    if not lema:
        # fallback to url slug
        url_last2: str = source_ref.url.rstrip("/").split("/")[-1]
        try:
            from urllib.parse import unquote

            url_last2 = unquote(url_last2)
        except Exception:
            pass
        lema = _clean_text(url_last2)
        if lema.islower():
            lema = lema[:1].upper() + lema[1:]
        if not lema:
            raise MissingFieldError(f"lema not found in JSON for {source_ref.url}")

    makna_raw: Any = _get_first(
        payload,
        [
            "makna",
            "arti",
            "definisi",
            "definitions",
            "meanings",
            "senses",
            "definition",
            "sense",
        ],
    )
    makna: list[dict[str, Any]] = _normalize_makna(makna_raw)
    if not makna:
        raise MissingFieldError(f"makna not found or empty for lema {lema!r}")

    contoh_raw: Any = _get_first(
        payload,
        ["contoh", "examples", "example", "kalimat", "contohKalimat", "sentences"],
    )
    contoh: list[str] = _normalize_contoh(contoh_raw)

    bentuk_raw: Any = _get_first(
        payload,
        [
            "bentukTidakBaku",
            "bentuk_tidak_baku",
            "tidakBaku",
            "varian",
            "nonstandard",
            "variant",
            "bentukTidakBakuList",
        ],
    )
    bentuk_tidak_baku: list[str] = _normalize_bentuk_tidak_baku(bentuk_raw)

    kelas_raw: Any = _get_first(
        payload, ["kelasKata", "kelas_kata", "kelas", "pos", "wordClass", "word_class"]
    )
    kelas_kata: list[str] = _normalize_kelas_kata(kelas_raw)

    # Optional fields
    bentuk_baku_raw: Any = _get_first(
        payload, ["bentukBaku", "bentuk_baku", "standardForm", "baku"]
    )
    bentuk_baku: str | None = None
    if isinstance(bentuk_baku_raw, str):
        txt: str = _clean_text(bentuk_baku_raw)
        if txt:
            bentuk_baku = txt

    etimologi_raw: Any = _get_first(payload, ["etimologi", "etymology"])
    etimologi: str | None = None
    if isinstance(etimologi_raw, str):
        etimologi = _clean_text(etimologi_raw) or None

    # Build id
    entry_id: str = lema.strip().lower()
    entry_id = re.sub(r"\s+", "-", entry_id)
    entry_id = re.sub(r"[^a-z0-9\-_]", "", entry_id)
    if not entry_id:
        entry_id = lema.strip().lower()

    try:
        entry: KBBIEntry = KBBIEntry(
            id=entry_id,
            lema=lema,
            makna=makna,
            kelas_kata=kelas_kata,
            contoh=contoh,
            bentuk_tidak_baku=bentuk_tidak_baku,
            bentuk_baku=bentuk_baku,
            etimologi=etimologi,
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


parse_kbbi_json = parse_kbbi
