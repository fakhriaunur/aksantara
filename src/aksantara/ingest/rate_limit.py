"""Rate limiting for Aksantara ingest — token bucket + bounded concurrency.

Pure, deterministic helpers with no I/O. Transport modules call
`RateLimiter.acquire()` before each fetch and handle retries via
`calculate_backoff`.

Phase 2: in-memory only, low concurrency (2), rate ~1 rps per source.
No Vertex/Firestore coupling.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class TokenBucket:
    """Thread-safe token bucket for rate limiting.

    Attributes:
        rate: tokens added per second.
        capacity: maximum burst tokens.
    """

    def __init__(
        self,
        rate_per_second: float,
        capacity: float,
        *,
        time_source: Any | None = None,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.rate_per_second: float = float(rate_per_second)
        self.capacity: float = float(capacity)
        self._tokens: float = float(capacity)
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()
        # allow injecting monotonic for tests (callable)
        self._time_source: Any = time.monotonic if time_source is None else time_source

    def _refill(self) -> None:
        now: float = self._time_source()
        elapsed: float = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self.capacity, self._tokens + elapsed * self.rate_per_second
            )
            self._last_refill = now

    def try_consume(self, tokens: float = 1.0) -> bool:
        """Try to consume tokens without waiting. Returns True if allowed."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def consume_blocking(
        self, tokens: float = 1.0, timeout: float | None = None
    ) -> bool:
        """Block until tokens available or timeout exceeded. Returns True if consumed."""
        deadline: float | None = None
        if timeout is not None:
            deadline = self._time_source() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                # need to wait
                needed: float = tokens - self._tokens
                wait: float = needed / self.rate_per_second
            # clamp wait to small sleep to avoid busy-spin
            wait = min(wait, 0.1)
            if deadline is not None and self._time_source() + wait > deadline:
                return False
            time.sleep(wait)

    def wait_time(self, tokens: float = 1.0) -> float:
        """Seconds until `tokens` are available without consuming."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                return 0.0
            needed: float = tokens - self._tokens
            return needed / self.rate_per_second

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


# ---------------------------------------------------------------------------
# Rate limiter + concurrency + retry helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: bool = True


def calculate_backoff(
    attempt: int, base_delay: float = 0.5, max_delay: float = 8.0, jitter: bool = True
) -> float:
    """Exponential backoff with optional jitter.

    Args:
        attempt: zero-based retry index (0 = first retry).
        base_delay: initial delay in seconds.
        max_delay: cap in seconds.
        jitter: if True add 0-10% jitter to avoid thundering herd.

    Returns:
        Delay in seconds.
    """
    delay: float = base_delay * (2**attempt)
    delay = min(delay, max_delay)
    if jitter:
        # 0-10% jitter, deterministic per call via random (not crypto)
        delay = delay * (1.0 + random.random() * 0.1)
    return delay


def is_retryable_status(status_code: int) -> bool:
    """True for 429 or 5xx which should be retried."""
    return status_code == 429 or 500 <= status_code < 600


class RateLimiter:
    """Combines token bucket and bounded concurrency semaphore.

    Low concurrency defaults match KBBI fetch policy: max 2 concurrent,
    ~1 request/sec per source, bounded retries with backoff.
    """

    def __init__(
        self,
        rate_per_second: float = 1.0,
        capacity: float = 2.0,
        max_concurrency: int = 2,
        retry_config: RetryConfig | None = None,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        self.bucket: TokenBucket = TokenBucket(rate_per_second, capacity)
        self.semaphore: threading.Semaphore = threading.Semaphore(max_concurrency)
        self.retry_config: RetryConfig = retry_config or RetryConfig()
        self.rate_per_second: float = rate_per_second
        self.capacity: float = capacity
        self.max_concurrency: int = max_concurrency

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire concurrency slot and a token. Blocks until available."""
        # Acquire semaphore first (bounded concurrency)
        if timeout is None:
            acquired: bool = self.semaphore.acquire()
        else:
            acquired = self.semaphore.acquire(timeout=timeout)
        if not acquired:
            return False
        # Then consume token (may block for rate)
        ok: bool = self.bucket.consume_blocking(timeout=timeout)
        if not ok:
            self.semaphore.release()
            return False
        return True

    def release(self) -> None:
        """Release concurrency slot. Token already consumed."""
        try:
            self.semaphore.release()
        except ValueError:
            pass

    def __enter__(self) -> RateLimiter:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.release()

    def backoff_for_attempt(self, attempt: int) -> float:
        return calculate_backoff(
            attempt,
            base_delay=self.retry_config.base_delay,
            max_delay=self.retry_config.max_delay,
            jitter=self.retry_config.jitter,
        )


# ---------------------------------------------------------------------------
# Global defaults (low-rate per spec)
# ---------------------------------------------------------------------------

DEFAULT_RATE_LIMITER: RateLimiter = RateLimiter(
    rate_per_second=1.0, capacity=2.0, max_concurrency=2
)
OFFICIAL_RATE_LIMITER: RateLimiter = RateLimiter(
    rate_per_second=1.0, capacity=2.0, max_concurrency=2
)
FALLBACK_RATE_LIMITER: RateLimiter = RateLimiter(
    rate_per_second=2.0, capacity=2.0, max_concurrency=2
)

__all__ = [
    "DEFAULT_RATE_LIMITER",
    "FALLBACK_RATE_LIMITER",
    "OFFICIAL_RATE_LIMITER",
    "RateLimiter",
    "RetryConfig",
    "TokenBucket",
    "calculate_backoff",
    "is_retryable_status",
]
