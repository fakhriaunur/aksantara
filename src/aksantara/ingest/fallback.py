"""Fallback KBBI fetcher — mirror/gov-derived sources.

Labeled source_kind fallback or gov-derived (never official-live).
Same parser contract as official fetcher: returns (bytes, SourceRef)
with contentHash, edition VI, retrievedAt UTC, parser_version 0.1.0.

Primary mirror: https://kbbi.web.id/{lema}
Gov-derived dumps may use same interface with source_kind=gov-derived
and an alternate base_url.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

import httpx

from aksantara.domain.models import SourceRef
from aksantara.domain.provenance import content_hash_bytes
from aksantara.ingest.rate_limit import (
    FALLBACK_RATE_LIMITER,
    RateLimiter,
    calculate_backoff,
    is_retryable_status,
)

PARSER_VERSION: str = "0.1.0"
FALLBACK_BASE_URL: str = "https://kbbi.web.id"
GOV_DERIVED_BASE_URL: str = "https://kbbi.web.id"
FALLBACK_EDITION: str = "VI"
FALLBACK_SOURCE_VERSION: str = "VI"
DEFAULT_TIMEOUT: float = 10.0
DEFAULT_MAX_RETRIES: int = 3

FallbackKind = Literal["fallback", "gov-derived"]


def _build_fallback_url(lema: str, base_url: str) -> str:
    slug: str = lema.strip()
    if not slug:
        raise ValueError("lema must be non-empty")
    encoded: str = quote(slug, safe="")
    # kbbi.web.id uses /{lema} or /entri/{lema} style; normalize
    base: str = base_url.rstrip("/")
    return f"{base}/{encoded}"


def _make_fallback_source_ref(
    url: str, raw_bytes: bytes, source_kind: FallbackKind
) -> SourceRef:
    ch: str = content_hash_bytes(raw_bytes)
    now: datetime = datetime.now(UTC)
    return SourceRef(
        url=url,
        source_kind=source_kind,
        edition=FALLBACK_EDITION,
        source_version=FALLBACK_SOURCE_VERSION,
        retrieved_at=now,
        content_hash=ch,
        parser_version=PARSER_VERSION,
    )


def fetch_fallback(
    lema: str,
    *,
    source_kind: FallbackKind = "fallback",
    base_url: str | None = None,
    client: httpx.Client | None = None,
    rate_limiter: RateLimiter | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[bytes, SourceRef]:
    """Fetch fallback KBBI entry for `lema`.

    Args:
        lema: headword.
        source_kind: "fallback" (mirror) or "gov-derived" (community dump).
        base_url: override mirror base URL (for testing or gov-derived snapshot).
        client: optional shared httpx.Client.
        rate_limiter: optional RateLimiter; defaults to FALLBACK_RATE_LIMITER.
        timeout: per-request timeout.
        max_retries: bounded retries.

    Returns:
        (raw_bytes, SourceRef) labeled with fallback source_kind.

    Raises:
        ValueError if lema empty or source_kind invalid.
        httpx.HTTPError on failure.
    """
    if source_kind not in ("fallback", "gov-derived"):
        raise ValueError(
            f"source_kind must be fallback or gov-derived, got {source_kind!r}"
        )
    effective_base: str = (
        base_url
        if base_url is not None
        else (
            GOV_DERIVED_BASE_URL if source_kind == "gov-derived" else FALLBACK_BASE_URL
        )
    )
    url: str = _build_fallback_url(lema, effective_base)
    limiter: RateLimiter = rate_limiter or FALLBACK_RATE_LIMITER
    headers: dict[str, str] = {"User-Agent": "Aksantara/0.1.0 (+https://kbbi.web.id)"}
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        acquired: bool = limiter.acquire(timeout=30.0)
        if not acquired:
            raise RuntimeError("rate limiter acquire timed out")
        try:
            try:
                if client is not None:
                    resp: httpx.Response = client.get(
                        url, timeout=timeout, follow_redirects=True, headers=headers
                    )
                else:
                    with httpx.Client(
                        timeout=timeout, follow_redirects=True, headers=headers
                    ) as tmp_client:
                        resp = tmp_client.get(url)
            except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay: float = calculate_backoff(attempt)
                    time.sleep(delay)
                    continue
                raise

            if is_retryable_status(resp.status_code):
                last_exc = httpx.HTTPStatusError(
                    f"retryable status {resp.status_code} for {url}",
                    request=resp.request,
                    response=resp,
                )
                if attempt < max_retries:
                    delay = calculate_backoff(attempt)
                    retry_after: str | None = resp.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                    time.sleep(delay)
                    continue
                resp.raise_for_status()

            resp.raise_for_status()
            raw: bytes = resp.content
            source_ref: SourceRef = _make_fallback_source_ref(url, raw, source_kind)
            return (raw, source_ref)
        finally:
            limiter.release()

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"fetch_fallback exhausted retries for {url}")


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "FALLBACK_BASE_URL",
    "FALLBACK_EDITION",
    "GOV_DERIVED_BASE_URL",
    "PARSER_VERSION",
    "FallbackKind",
    "fetch_fallback",
]
