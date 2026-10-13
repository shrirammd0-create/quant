"""One HTTP helper shared by the data loader and the Telegram notifier.

``request_with_retry`` retries network errors and transient HTTP statuses with
jittered exponential backoff, honours ``Retry-After``, and gives up once an
optional deadline is reached so a full outage cannot hang the job.
"""
from __future__ import annotations

import email.utils
import random
import time

import requests

# Statuses worth retrying: timeouts, rate limiting and server-side errors.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Misconfiguration (bad URL, bad scheme, redirect loop): retrying cannot help.
_PERMANENT_ERRORS = (ValueError, requests.exceptions.TooManyRedirects)


class RequestFailed(Exception):
    """Every attempt failed. ``reason`` never contains the URL (it may hold a token)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:  # HTTP-date form
            return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def request_with_retry(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    attempts: int = 5,
    timeout: tuple[float, float] = (10, 30),
    max_wait: float = 30.0,
    deadline: float | None = None,
    sleep=time.sleep,
    **kwargs,
) -> requests.Response:
    """Send the request; return the first response whose status is not retryable.

    Non-retryable statuses (200, 404, 401, ...) are returned for the caller to
    judge. Raises ``RequestFailed`` when attempts run out, when waiting for the
    next attempt would pass ``deadline`` (a ``time.monotonic()`` value), or
    immediately for configuration errors such as a malformed URL.
    """
    client = session or requests
    reason = "no attempt made"
    for attempt in range(attempts):
        wait = None
        try:
            response = client.request(method, url, timeout=timeout, **kwargs)
        except _PERMANENT_ERRORS as exc:
            raise RequestFailed(type(exc).__name__) from None
        except requests.RequestException as exc:  # connection reset, timeout, TLS, ...
            reason = type(exc).__name__
        else:
            if response.status_code not in RETRYABLE_STATUS:
                return response
            reason = f"HTTP {response.status_code}"
            wait = _retry_after(response)

        if attempt == attempts - 1:
            break
        if wait is None:
            ceiling = min(max_wait, 1.5 * 2**attempt)
            wait = ceiling / 2 + random.uniform(0, ceiling / 2)  # equal jitter
        wait = min(wait, max_wait)
        if deadline is not None and time.monotonic() + wait > deadline:
            break
        sleep(wait)
    raise RequestFailed(reason)
