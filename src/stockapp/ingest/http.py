"""Polite, resilient HTTP for connectors.

* Browser-like headers (NSE refuses obvious bots).
* A minimum interval between requests (rate limit).
* Retries with exponential backoff on 429, 5xx and network errors.
* 404 means "no file published" and is reported as ``NotAvailable``, never retried.
* A circuit breaker: after ``breaker_threshold`` consecutive failures the client stops calling the
  source for the rest of the run, so a blocked source doesn't get hammered.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(RuntimeError):
    """The source could not be fetched (after retries)."""


class NotAvailable(FetchError):
    """The source answered that this file doesn't exist (HTTP 404)."""


class Blocked(FetchError):
    """The source refused us (HTTP 401/403): likely bot protection or an IP block."""


class CircuitOpen(FetchError):
    """Too many consecutive failures in this run; the client has stopped calling the source."""


class PoliteClient:
    def __init__(
        self,
        *,
        min_interval_s: float = 1.0,
        retries: int = 3,
        backoff_s: float = 2.0,
        breaker_threshold: int = 5,
        timeout_s: float = 30.0,
        headers: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_interval_s = min_interval_s
        self.retries = retries
        self.backoff_s = backoff_s
        self.breaker_threshold = breaker_threshold
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.consecutive_failures = 0
        self._client = client or httpx.Client(
            headers={**BROWSER_HEADERS, **(headers or {})},
            timeout=timeout_s,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        if self.consecutive_failures >= self.breaker_threshold:
            raise CircuitOpen(
                f"{self.consecutive_failures} consecutive failures; not calling {url}"
            )
        last_error = ""
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url, **kwargs)  # type: ignore[arg-type]
            except httpx.TransportError as exc:
                last_error = type(exc).__name__
            else:
                if resp.status_code == 200:
                    self.consecutive_failures = 0
                    return resp
                if resp.status_code == 404:
                    self.consecutive_failures = 0  # the source is up; the file just isn't there
                    raise NotAvailable(f"HTTP 404 for {url}")
                if resp.status_code in (401, 403):
                    self.consecutive_failures += 1
                    raise Blocked(f"HTTP {resp.status_code} for {url}")
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code != 429 and resp.status_code < 500:
                    break
            if attempt < self.retries:
                self._sleep(self.backoff_s * 2**attempt)
        self.consecutive_failures += 1
        raise FetchError(f"{last_error} for {url} after {self.retries + 1} attempts")

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            wait = self.min_interval_s - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_request = now
