"""Cost guardrails for AWS Bedrock usage (rate limiting + global budgets).

Both `/api/v1/hint` and `/api/v1/story` default `use_bedrock=True`, and a
retry-happy frontend (or an anonymous caller — the hint route is public until
auth lands there) could otherwise trigger unbounded `invoke_model` calls and
an unexpected AWS bill. This module is the single place that:

- enforces a per-principal token-bucket rate limit (N Bedrock-backed requests
  per minute; "principal" is the authenticated account or student when the
  request carries one, else the client IP) — enforced at the API layer, which
  answers HTTP 429 with a Retry-After header;
- maintains global daily and monthly invocation budgets with a hard cutover:
  once either budget is exhausted, every Bedrock-backed feature silently
  degrades to its deterministic template fallback until the window resets,
  and the cutover is logged exactly once per exhausted window. Budgets are
  consumed at dispatch time (just before `invoke_model`), so failed or
  retried provider calls cannot bypass the cap;
- exposes a usage snapshot for the admin endpoint
  (`GET /api/v1/admin/bedrock-usage`) without leaking any student identifier.

All limits are environment-configurable (see README, "Bedrock cost
guardrails"). Counters are in-memory: they bound spend per process lifetime
per calendar window and reset on restart, which is the right trade-off for a
single-process deployment; horizontal deployments should front this with a
shared limiter.
"""

import math
import os
import threading
import time
from datetime import UTC, datetime

from agent.log_config import get_logger

logger = get_logger(__name__)

DEFAULT_RATE_LIMIT_PER_MINUTE = 10
DEFAULT_DAILY_BUDGET = 1000
DEFAULT_MONTHLY_BUDGET = 20000

RATE_LIMIT_WINDOW_SECONDS = 60.0

# Safety valve against unbounded memory growth from per-IP buckets: when more
# than this many principals are tracked, stale/idle buckets are pruned on the
# next acquire.
_MAX_TRACKED_PRINCIPALS = 10_000


class RateLimitExceededError(Exception):
    """Raised when a principal exceeds the per-minute Bedrock rate limit."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Bedrock rate limit exceeded; retry after {retry_after_seconds}s."
        )


class _TokenBucket:
    """Classic token bucket: burst up to capacity, refilling linearly."""

    __slots__ = ("capacity", "tokens", "updated_at")

    def __init__(self, capacity: float, now: float):
        self.capacity = capacity
        self.tokens = capacity
        self.updated_at = now

    def try_acquire(self, now: float) -> tuple[bool, int]:
        """Attempt to take one token; return (ok, retry_after_seconds)."""
        if self.capacity <= 0:
            # Emergency-stop configuration (BEDROCK_RATE_LIMIT_PER_MINUTE=0):
            # nothing is ever allowed; suggest retrying after a full window.
            return False, int(RATE_LIMIT_WINDOW_SECONDS)
        refill_per_second = self.capacity / RATE_LIMIT_WINDOW_SECONDS
        self.tokens = min(
            self.capacity, self.tokens + (now - self.updated_at) * refill_per_second
        )
        self.updated_at = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0
        deficit = 1.0 - self.tokens
        return False, max(1, math.ceil(deficit / refill_per_second))


_lock = threading.Lock()
_buckets: dict[str, _TokenBucket] = {}


# --------------------------------------------------------------------------
# Configuration (environment variables)
# --------------------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    """Parse a limit env var; negative values mean 'unlimited/disabled'."""
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def get_rate_limit_per_minute() -> int:
    """Max Bedrock-backed requests per principal per minute (-1 disables)."""
    return _int_env("BEDROCK_RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE)


def get_daily_budget() -> int:
    """Global Bedrock invocations allowed per UTC day (-1 = unlimited)."""
    return _int_env("BEDROCK_DAILY_BUDGET", DEFAULT_DAILY_BUDGET)


def get_monthly_budget() -> int:
    """Global Bedrock invocations allowed per UTC month (-1 = unlimited)."""
    return _int_env("BEDROCK_MONTHLY_BUDGET", DEFAULT_MONTHLY_BUDGET)


# --------------------------------------------------------------------------
# Per-principal rate limiting (API layer → HTTP 429)
# --------------------------------------------------------------------------

def _prune_stale_buckets(now: float) -> None:
    """Bound memory: drop idle buckets when tracking too many principals."""
    if len(_buckets) <= _MAX_TRACKED_PRINCIPALS:
        return
    stale_cutoff = now - 2 * RATE_LIMIT_WINDOW_SECONDS
    stale = [key for key, b in _buckets.items() if b.updated_at < stale_cutoff]
    for key in stale:
        del _buckets[key]
    if len(_buckets) > _MAX_TRACKED_PRINCIPALS:
        # Still full of fresh buckets (heavy traffic): evict oldest first.
        ordered = sorted(_buckets.items(), key=lambda item: item[1].updated_at)
        excess = len(_buckets) - int(_MAX_TRACKED_PRINCIPALS * 0.9)
        for key, _ in ordered[:excess]:
            del _buckets[key]


def acquire_request_slot(principal: str) -> None:
    """Charge one Bedrock-backed request to `principal`'s token bucket.

    Raises RateLimitExceededError (with a Retry-After hint) when the
    principal's per-minute allowance is spent. A non-positive configured
    limit disables enforcement entirely.
    """
    limit = get_rate_limit_per_minute()
    if limit < 0:
        return
    now = time.monotonic()
    with _lock:
        bucket = _buckets.get(principal)
        if bucket is None or bucket.capacity != limit:
            # Rebuild when missing or when the configured limit changed.
            bucket = _TokenBucket(float(max(limit, 0)), now)
            _buckets[principal] = bucket
        ok, retry_after = bucket.try_acquire(now)
        _prune_stale_buckets(now)
    if not ok:
        logger.warning(
            "Bedrock rate limit exceeded for a principal",
            extra={
                "source_module": __name__,
                "source_function": "acquire_request_slot",
                "provider_outcome": "rate_limited",
                "retry_after_seconds": retry_after,
            },
        )
        raise RateLimitExceededError(retry_after)


# --------------------------------------------------------------------------
# Global budgets (generator layer → template-only fallback)
# --------------------------------------------------------------------------

# Counters are keyed implicitly: they only ever hold the *current* UTC
# day/month, so a window reset is just "the date changed". The cutover-log
# markers work the same way and are dropped when their window rolls over so
# each exhausted window logs exactly once.
_daily_count = 0
_monthly_count = 0
_current_day = ""
_current_month = ""
_cutover_logged_windows: set[str] = set()


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _utc_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def try_consume_budget(feature: str) -> bool:
    """Atomically reserve one Bedrock invocation against the global budgets.

    Returns True when the caller may proceed with `invoke_model`; returns
    False once either the daily or monthly budget is exhausted, logging the
    hard cutover to template-only mode once per exhausted window. The slot is
    consumed before the provider call so retries/failures cannot bypass the
    cap (deliberately conservative for billing).
    """
    global _daily_count, _monthly_count, _current_day, _current_month

    daily_limit = get_daily_budget()
    monthly_limit = get_monthly_budget()

    with _lock:
        today = _utc_day()
        if today != _current_day:
            _current_day = today
            _daily_count = 0
            _cutover_logged_windows.discard("daily")
        month = _utc_month()
        if month != _current_month:
            _current_month = month
            _monthly_count = 0
            _cutover_logged_windows.discard("monthly")

        over_daily = 0 <= daily_limit <= _daily_count
        over_monthly = 0 <= monthly_limit <= _monthly_count
        if over_daily or over_monthly:
            window = "daily" if over_daily else "monthly"
            if window not in _cutover_logged_windows:
                _cutover_logged_windows.add(window)
                logger.warning(
                    "Global Bedrock budget exhausted; serving template-only "
                    "fallback until the window resets",
                    extra={
                        "source_module": __name__,
                        "source_function": "try_consume_budget",
                        "feature": feature,
                        "budget_window": window,
                        "budget_limit": daily_limit if over_daily else monthly_limit,
                        "budget_used": _daily_count if over_daily else _monthly_count,
                        "outcome": "budget_exhausted",
                    },
                )
            return False

        # Count even when a limit is disabled (-1) so usage stays observable.
        _daily_count += 1
        _monthly_count += 1
    return True


def usage_snapshot() -> dict:
    """Current guardrail state for the admin endpoint.

    Contains only aggregates — never student identifiers or raw principals.
    """
    daily_limit = get_daily_budget()
    monthly_limit = get_monthly_budget()
    rate_limit = get_rate_limit_per_minute()
    with _lock:
        daily_used = _daily_count
        monthly_used = _monthly_count
        day = _current_day or _utc_day()
        month = _current_month or _utc_month()
        principals = len(_buckets)
    return {
        "daily": {
            "window": day,
            "used": daily_used,
            "limit": None if daily_limit < 0 else daily_limit,
        },
        "monthly": {
            "window": month,
            "used": monthly_used,
            "limit": None if monthly_limit < 0 else monthly_limit,
        },
        "budget_exhausted": (
            (0 <= daily_limit <= daily_used) or (0 <= monthly_limit <= monthly_used)
        ),
        "rate_limit": {"per_minute": None if rate_limit < 0 else rate_limit},
        "tracked_principals": principals,
    }


def reset_state() -> None:
    """Drop all counters and buckets so tests start from a clean slate."""
    global _buckets, _daily_count, _monthly_count, _current_day, _current_month

    with _lock:
        _buckets = {}
        _daily_count = 0
        _monthly_count = 0
        _current_day = ""
        _current_month = ""
        _cutover_logged_windows.clear()
