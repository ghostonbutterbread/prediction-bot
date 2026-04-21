"""Shared lightweight rate limiting helpers for API-bound modules."""

# Kalshi account limits endpoint: GET /account/limits

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

ACCOUNT_TIER_LIMITS = {
    "basic": {"reads_per_second": 20.0, "writes_per_second": 10.0},
    "advanced": {"reads_per_second": 30.0, "writes_per_second": 30.0},
    "premier": {"reads_per_second": 100.0, "writes_per_second": 100.0},
    "prime": {"reads_per_second": 400.0, "writes_per_second": 400.0},
}


@dataclass
class RateLimitProfile:
    reads_per_second: float
    writes_per_second: float
    account_tier: str = "basic"

    @classmethod
    def from_values(cls, reads_per_second: float, writes_per_second: float, account_tier: str = "custom") -> "RateLimitProfile":
        return cls(
            reads_per_second=max(0.1, float(reads_per_second)),
            writes_per_second=max(0.1, float(writes_per_second)),
            account_tier=str(account_tier or "custom").strip().lower(),
        )

    @classmethod
    def from_account_tier(cls, tier: str | None) -> "RateLimitProfile":
        normalized = str(tier or "basic").strip().lower()
        limits = ACCOUNT_TIER_LIMITS.get(normalized, ACCOUNT_TIER_LIMITS["basic"])
        return cls(
            reads_per_second=float(limits["reads_per_second"]),
            writes_per_second=float(limits["writes_per_second"]),
            account_tier=normalized,
        )


class SlidingWindowRateLimiter:
    def __init__(self, max_calls_per_second: float):
        self.max_calls_per_second = max(0.1, float(max_calls_per_second))
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 1.0
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_calls_per_second:
                    self._timestamps.append(now)
                    return

                sleep_for = max(0.01, 1.0 - (now - self._timestamps[0]))
            time.sleep(sleep_for)


class RequestThrottle:
    def __init__(self, profile: RateLimitProfile):
        self.profile = profile
        self._read_limiter = SlidingWindowRateLimiter(profile.reads_per_second)
        self._write_limiter = SlidingWindowRateLimiter(profile.writes_per_second)

    def wait(self, kind: str) -> None:
        if str(kind).lower() == "write":
            self._write_limiter.wait()
        else:
            self._read_limiter.wait()

    def update_profile(self, profile: RateLimitProfile) -> None:
        self.profile = profile
        self._read_limiter = SlidingWindowRateLimiter(profile.reads_per_second)
        self._write_limiter = SlidingWindowRateLimiter(profile.writes_per_second)
        logger.info(
            "Updated request throttle profile: tier=%s reads/s=%s writes/s=%s",
            profile.account_tier,
            profile.reads_per_second,
            profile.writes_per_second,
        )


def call_with_retry(
    fn: Callable[[], T],
    *,
    throttle: Optional[RequestThrottle] = None,
    kind: str = "read",
    max_retries: int = 3,
    base_sleep_seconds: float = 1.0,
    retry_on: tuple[type[Exception], ...] = (httpx.RequestError,),
) -> T:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        if throttle is not None:
            throttle.wait(kind)
        try:
            return fn()
        except retry_on as exc:
            last_error = exc
            if attempt == max_retries - 1:
                raise
            sleep_for = base_sleep_seconds * (2 ** attempt)
            logger.warning("Request failed (%s). Retrying in %.1fs (%s/%s)", exc, sleep_for, attempt + 1, max_retries)
            time.sleep(sleep_for)
    if last_error is not None:
        raise last_error
    raise RuntimeError("call_with_retry exhausted unexpectedly")


def http_get_with_retry(
    url: str,
    headers: dict,
    *,
    throttle: Optional[RequestThrottle] = None,
    timeout: int = 10,
    max_retries: int = 3,
) -> Optional[httpx.Response]:
    for attempt in range(max_retries):
        if throttle is not None:
            throttle.wait("read")
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else float(2 ** attempt)
                logger.warning("Rate limited (429). Waiting %ss before retry %s/%s", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            return resp
        except httpx.RequestError as exc:
            if attempt == max_retries - 1:
                logger.error("HTTP request failed after %s attempts: %s", max_retries, exc)
                return None
            time.sleep(2 ** attempt)
    return None
