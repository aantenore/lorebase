"""HTTP client shared by the endpoint providers: timeouts, retries and backoff.

Speaks JSON over plain HTTP with `httpx`; no vendor library is involved. Calls are retried on
429, 500, 502, 503, 504 and on connection errors or timeouts, up to `retry.max_retries` times.
A `Retry-After` header (seconds or HTTP date) sets the wait, capped at `retry.backoff_max_s`;
otherwise the wait is exponential backoff with full jitter. Request and response bodies are
never logged: they hold prompts and documents.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import SecretStr

from lorebase_inference.errors import EndpointError
from lorebase_inference.settings import RetrySettings

logger = logging.getLogger("lorebase.inference")

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_DETAIL_LIMIT = 300
_CONNECT_TIMEOUT_S = 10.0

Sleep = Callable[[float], Awaitable[None]]


def parse_retry_after(
    value: str | None, *, now: Callable[[], datetime] | None = None
) -> float | None:
    """Seconds to wait according to a `Retry-After` header value, or `None` when unusable."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = float((moment - (now or (lambda: datetime.now(UTC)))()).total_seconds())
    return None if math.isnan(seconds) else max(seconds, 0.0)


def error_detail(response: httpx.Response) -> str:
    """Error text of a failed response: the API error message when present, else the body."""
    try:
        body = response.json()
    except ValueError:
        text = response.text
    else:
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            text = str(error.get("message") or error)
        elif error is not None:
            text = str(error)
        else:
            text = str(body)
    return " ".join(text.split())[:_DETAIL_LIMIT]


class EndpointClient:
    """JSON POST client for one HTTP endpoint with bounded concurrency and retries."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | None,
        timeout_s: float,
        retry: RetrySettings,
        max_concurrency: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must start with http:// or https://, got {base_url!r}")
        self._base_url = base_url.rstrip("/")
        self._key = api_key
        self._timeout = httpx.Timeout(timeout_s, connect=min(_CONNECT_TIMEOUT_S, timeout_s))
        self._retry = retry
        self._slots = asyncio.Semaphore(max_concurrency)
        self._transport = transport
        self._sleep: Sleep = sleep or asyncio.sleep
        self._jitter = jitter
        self._client: httpx.AsyncClient | None = None

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST `payload` as JSON to `path` under the base URL and return the JSON object."""
        client = self._http()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._key is not None and self._key.get_secret_value():
            headers["Authorization"] = f"Bearer {self._key.get_secret_value()}"
        async with self._slots:
            attempt = 0
            while True:
                started = time.monotonic()
                retry_after: float | None = None
                try:
                    response = await client.post(
                        self._base_url + path, json=payload, headers=headers
                    )
                except httpx.TransportError as exc:
                    reason = type(exc).__name__
                    failure = EndpointError(f"{path}: request failed ({reason})")
                else:
                    logger.debug(
                        "POST %s -> %d in %.0f ms (attempt %d)",
                        path,
                        response.status_code,
                        (time.monotonic() - started) * 1000,
                        attempt + 1,
                    )
                    if response.status_code < 400:
                        return _json_object(path, response)
                    reason = f"HTTP {response.status_code}"
                    detail = error_detail(response)
                    failure = EndpointError(
                        f"{path}: HTTP {response.status_code}: {detail}",
                        status=response.status_code,
                        detail=detail,
                    )
                    if response.status_code not in RETRYABLE_STATUS:
                        raise failure
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if attempt >= self._retry.max_retries:
                    raise failure
                delay = self._delay(attempt, retry_after)
                logger.warning(
                    "%s failed (%s); retry %d/%d in %.1f s",
                    path,
                    reason,
                    attempt + 1,
                    self._retry.max_retries,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        cap = self._retry.backoff_max_s
        if retry_after is not None:
            return min(retry_after, cap)
        return float(self._jitter() * min(cap, self._retry.backoff_base_s * 2**attempt))

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def _json_object(path: str, response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise EndpointError(f"{path}: response is not JSON", status=response.status_code) from None
    if not isinstance(body, dict):
        raise EndpointError(f"{path}: response is not a JSON object", status=response.status_code)
    return body
