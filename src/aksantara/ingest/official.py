"""Official KBBI fetcher — kbbi.kemdikbud.go.id/entri/{lema}.

Uses httpx 0.28.1, respects RateLimiter (token bucket + low concurrency),
bounded retries with exponential backoff, returns (bytes, SourceRef) with
contentHash, source_kind official-live, edition VI, retrievedAt UTC,
parser_version 0.1.0.

Pure transport: does not parse or interpret lexical content.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from aksantara.domain.models import SourceRef
from aksantara.domain.provenance import content_hash_bytes
from aksantara.ingest.rate_limit import (
    OFFICIAL_RATE_LIMITER,
    RateLimiter,
    calculate_backoff,
    is_retryable_status,
)

PARSER_VERSION: str = "0.1.0"
OFFICIAL_BASE_URL: str = "https://kbbi.kemdikbud.go.id/entri"
OFFICIAL_SOURCE_KIND: str = "official-live"
OFFICIAL_EDITION: str = "VI"
OFFICIAL_SOURCE_VERSION: str = "VI"
DEFAULT_TIMEOUT: float = 10.0
DEFAULT_MAX_RETRIES: int = 3


def _build_url(lema: str) -> str:
    slug: str = lema.strip()
    if not slug:
        raise ValueError("lema must be non-empty")
    # KBBI URL uses lower-cased slug with url encoding; preserve original case for fetch
    encoded: str = quote(slug, safe="")
    return f"{OFFICIAL_BASE_URL}/{encoded}"


def _make_source_ref(url: str, raw_bytes: bytes) -> SourceRef:
    ch: str = content_hash_bytes(raw_bytes)
    now: datetime = datetime.now(UTC)
    return SourceRef(
        url=url,
        source_kind="official-live",
        edition=OFFICIAL_EDITION,
        source_version=OFFICIAL_SOURCE_VERSION,
        retrieved_at=now,
        content_hash=ch,
        parser_version=PARSER_VERSION,
    )


def fetch_official(
    lema: str,
    *,
    client: httpx.Client | None = None,
    rate_limiter: RateLimiter | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[bytes, SourceRef]:
    """Fetch official KBBI entry for `lema`.

    Args:
        lema: headword, e.g. "Februari".
        client: optional shared httpx.Client (caller owns lifecycle).
        rate_limiter: optional RateLimiter; defaults to OFFICIAL_RATE_LIMITER.
        timeout: per-request timeout seconds.
        max_retries: bounded retries for 429/5xx/network errors.

    Returns:
        (raw_bytes, SourceRef) with provenance-filled SourceRef.

    Raises:
        httpx.HTTPError on non-retryable or exhausted retries.
        ValueError if lema empty.
    """
    url: str = _build_url(lema)
    limiter: RateLimiter = rate_limiter or OFFICIAL_RATE_LIMITER
    headers: dict[str, str] = {
        "User-Agent": "Aksantara/0.1.0 (+https://kbbi.kemdikbud.go.id)"
    }

    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        # Rate-limit + concurrency gate (blocking)
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

            # Retry on 429 / 5xx
            if is_retryable_status(resp.status_code):
                last_exc = httpx.HTTPStatusError(
                    f"retryable status {resp.status_code} for {url}",
                    request=resp.request,
                    response=resp,
                )
                if attempt < max_retries:
                    delay = calculate_backoff(attempt)
                    # honor Retry-After if present
                    retry_after: str | None = resp.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                    time.sleep(delay)
                    continue
                resp.raise_for_status()

            # Non-retryable error: raise
            resp.raise_for_status()
            raw: bytes = resp.content
            source_ref: SourceRef = _make_source_ref(url, raw)
            return (raw, source_ref)

        finally:
            limiter.release()

    # Should be unreachable; raise last exception if exhausted
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"fetch_official exhausted retries for {url}")


# Convenience alias for callers expecting snake_case module function
__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "OFFICIAL_BASE_URL",
    "OFFICIAL_EDITION",
    "OFFICIAL_SOURCE_KIND",
    "OFFICIAL_SOURCE_VERSION",
    "PARSER_VERSION",
    "fetch_official",
]
