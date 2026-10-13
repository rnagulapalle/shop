"""Shared HTTP retry loop for provider calls.

Every retried request carries the same idempotency key, so a retry after a
lost response or a timeout returns the original result instead of repeating
the side effect.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {409, 429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 5.0


@dataclass
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    sleep: Callable[[float], None] = time.sleep


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def send_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    policy: RetryPolicy,
    should_retry: Callable[[httpx.Response], bool] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying transport errors and retryable statuses.

    Returns the last response (which may still be an error status) or raises
    the last ``httpx.TransportError`` once retries are exhausted.
    """
    should_retry = should_retry or (lambda r: r.status_code in RETRYABLE_STATUS)
    attempt = 0
    while True:
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= policy.max_retries:
                raise
            delay = policy.base_delay * (2**attempt)
            log.warning("%s %s failed (%s); retry %d in %.1fs", method, url, exc, attempt + 1, delay)
        else:
            if not should_retry(response) or attempt >= policy.max_retries:
                return response
            delay = _retry_after(response) or policy.base_delay * (2**attempt)
            log.warning(
                "%s %s returned %d; retry %d in %.1fs",
                method, url, response.status_code, attempt + 1, delay,
            )
        policy.sleep(min(delay, MAX_BACKOFF_SECONDS))
        attempt += 1
