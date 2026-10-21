"""The request layer shared by the sync and async clients.

It is sans-IO: a `Call` describes one API request; an `Exchange` turns it into `httpx` request
arguments and decides, for each outcome, whether the answer is final or the request is sent
again after a wait. The clients only run the loop (send, sleep) with their own I/O:

    exchange = Exchange(core, call)
    while True:
        try:
            response = http.send(http.build_request(**exchange.request_args()))
        except httpx.TransportError as error:
            step = exchange.on_error(error)
        else:
            step = exchange.on_response(response)
        if isinstance(step, Done):
            return step.value
        sleep(step.delay)

Retries (policy in `config.RetryPolicy`):

- 429 and, for problem documents, 503 are retried for every request: the server refused them
  before doing anything. A 503 that is not a problem document (a proxy) is retried only for
  requests that are safe to repeat.
- Connection and pool failures, where the request was never sent, are retried for every request.
- Other transport failures (timeouts while reading, resets) are retried only for requests that
  are safe to repeat: reads, deletes and writes carrying an idempotency key.
"""

from __future__ import annotations

import random
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from pydantic import BaseModel

from lorebase_client.config import ClientConfig
from lorebase_client.errors import (
    LorebaseConnectionError,
    error_from_response,
    is_problem,
    parse_retry_after,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
_KEY_CHARS = re.compile(r"[^A-Za-z0-9._:-]")
_KEY_MAX = 255


def new_idempotency_key(prefix: str | None = None) -> str:
    """A fresh key for a write, valid for the `Idempotency-Key` header.

    Retrying a write with the same key returns the original result instead of repeating it.
    """
    token = uuid.uuid4().hex
    if not prefix:
        return token
    return f"{_KEY_CHARS.sub('-', prefix)[: _KEY_MAX - len(token) - 1]}-{token}"


@dataclass(frozen=True)
class Call[T]:
    """One API request and how to read its answer."""

    method: str
    path: str
    parse: Callable[[httpx.Response], T]
    params: Mapping[str, Any] = field(default_factory=dict)
    body: Any = None
    """JSON body."""
    content: str | None = None
    """Raw text body (used instead of `body`) with `content_type`."""
    content_type: str | None = None
    files: tuple[tuple[str, str, bytes], ...] | None = None
    """Multipart upload (used instead of `body`): form field, file name and content."""
    form: Mapping[str, str] = field(default_factory=dict)
    """Text fields sent along with `files`."""
    accept: str = "application/json"
    safe: bool = False
    """True when sending the request twice is harmless (reads, deletes)."""
    accepts_key: bool = False
    """True for writes that honor an `Idempotency-Key`."""
    idempotency_key: str | None = None
    cacheable: bool = False
    """GET reads that carry an ETag; used by the opt-in cache."""

    def with_params(self, **updates: Any) -> Call[T]:
        """The same call with query parameters replaced (for example the page cursor)."""
        return replace(self, params={**self.params, **updates})

    def with_key(self, key: str | None) -> Call[T]:
        return replace(self, idempotency_key=key)

    @property
    def repeatable(self) -> bool:
        """Whether a request whose fate is unknown may be sent again."""
        return self.safe or self.idempotency_key is not None


def parse_model[M: BaseModel](model: type[M]) -> Callable[[httpx.Response], M]:
    """Parser of a JSON body into a response model."""
    return lambda response: model.model_validate_json(response.content)


def parse_none(response: httpx.Response) -> None:
    """Parser of answers without a body."""
    return None


def parse_text(response: httpx.Response) -> str:
    """Parser of text answers (the YAML schema)."""
    return response.text


def parse_json(response: httpx.Response) -> Any:
    """Parser of JSON answers kept as plain data."""
    return response.json()


@dataclass(frozen=True)
class Done[T]:
    """The final value of a call."""

    value: T


@dataclass(frozen=True)
class Retry:
    """Send the request again after `delay` seconds."""

    delay: float


@dataclass(frozen=True)
class _Entry:
    content: bytes
    headers: tuple[tuple[str, str], ...]


class ETagCache:
    """In-memory LRU of GET answers keyed by URL, revalidated with `If-None-Match`.

    The server binds the `ETag` of a read to the Revision it was served from, so an entry stays
    valid until the branch moves; the server answers 304 and the cached body is returned.
    """

    def __init__(self, max_entries: int) -> None:
        self._max = max_entries
        self._entries: OrderedDict[tuple[Any, ...], tuple[str, _Entry]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: tuple[Any, ...]) -> tuple[str, _Entry] | None:
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
            return found

    def put(self, key: tuple[Any, ...], etag: str, response: httpx.Response) -> None:
        entry = _Entry(response.content, tuple(response.headers.items()))
        with self._lock:
            self._entries[key] = (etag, entry)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class RequestCore:
    """Configuration, credentials, cache and clock of retries shared by one client."""

    def __init__(self, config: ClientConfig, *, rng: Callable[[], float] = random.random) -> None:
        self.config = config
        self.cache = None if config.cache_entries is None else ETagCache(config.cache_entries)
        self._rng = rng

    def url(self, path: str) -> str:
        return self.config.root + path

    def headers(self, call: Call[Any]) -> dict[str, str]:
        headers = {"Accept": call.accept, **self.config.headers}
        bearer = self.config.bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if call.content_type:
            headers["Content-Type"] = call.content_type
        if call.idempotency_key:
            headers[IDEMPOTENCY_HEADER] = call.idempotency_key
        return headers

    def delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before retry number `attempt + 1`."""
        policy = self.config.retry
        if retry_after is not None:
            return min(retry_after, policy.backoff_max_s)
        computed = min(policy.backoff_max_s, policy.backoff_base_s * float(2**attempt))
        return computed * (1.0 - policy.jitter * self._rng())


class Exchange[T]:
    """The state of one call across its attempts."""

    def __init__(self, core: RequestCore, call: Call[T]) -> None:
        if call.accepts_key and call.idempotency_key is None and core.config.auto_idempotency_keys:
            call = call.with_key(new_idempotency_key())
        self._core = core
        self.call = call
        self._attempt = 0
        self._cache_key = self._key() if self._uses_cache() else None

    def _uses_cache(self) -> bool:
        return self._core.cache is not None and self.call.cacheable and self.call.method == "GET"

    def _key(self) -> tuple[Any, ...]:
        params = tuple(sorted((k, str(v)) for k, v in _flatten(self.call.params) if v is not None))
        return (self._core.url(self.call.path), self.call.accept, params)

    def request_args(self) -> dict[str, Any]:
        """Keyword arguments of `build_request`."""
        call = self.call
        headers = self._core.headers(call)
        if self._cache_key is not None and self._core.cache is not None:
            cached = self._core.cache.get(self._cache_key)
            if cached is not None:
                headers["If-None-Match"] = cached[0]
        args: dict[str, Any] = {
            "method": call.method,
            "url": self._core.url(call.path),
            "params": _query(call.params),
            "headers": headers,
        }
        if call.files is not None:
            args["files"] = [(name, (filename, data)) for name, filename, data in call.files]
            if call.form:
                args["data"] = dict(call.form)
        elif call.content is not None:
            args["content"] = call.content.encode()
        elif call.body is not None:
            args["json"] = call.body
        return args

    def on_response(self, response: httpx.Response) -> Done[T] | Retry:
        """Final value, or a retry; raises the typed error for a final failure."""
        status = response.status_code
        if status == 304 and self._cache_key is not None and self._core.cache is not None:
            cached = self._core.cache.get(self._cache_key)
            if cached is not None:
                replay = httpx.Response(
                    200,
                    headers=list(cached[1].headers),
                    content=cached[1].content,
                    request=response.request,
                )
                return Done(self.call.parse(replay))
        if status >= 400:
            return self._failed(response)
        if self._cache_key is not None and self._core.cache is not None:
            etag = response.headers.get("etag")
            if etag and status == 200:
                self._core.cache.put(self._cache_key, etag, response)
        return Done(self.call.parse(response))

    def _failed(self, response: httpx.Response) -> Retry:
        policy = self._core.config.retry
        status = response.status_code
        retryable = status in policy.statuses and (
            status != 503 or self.call.repeatable or is_problem(response)
        )
        if retryable and self._attempt < policy.max_retries:
            wait = self._core.delay(
                self._attempt, parse_retry_after(response.headers.get("retry-after"))
            )
            self._attempt += 1
            return Retry(wait)
        raise error_from_response(response)

    def on_error(self, error: httpx.TransportError) -> Retry:
        """A retry for a failure that may be repeated; raises `LorebaseConnectionError` else."""
        policy = self._core.config.retry
        never_sent = isinstance(
            error, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout
        )
        if (never_sent or self.call.repeatable) and self._attempt < policy.max_retries:
            wait = self._core.delay(self._attempt, None)
            self._attempt += 1
            return Retry(wait)
        text = str(error) or type(error).__name__
        raise LorebaseConnectionError(
            f"cannot reach the server at {self._core.config.base_url}: {text}"
        ) from error


def _flatten(params: Mapping[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for name, value in params.items():
        if isinstance(value, list | tuple):
            out.extend((name, item) for item in value)
        else:
            out.append((name, value))
    return out


def _query(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Query string members: unset ones dropped, booleans as `true` and `false`."""
    out: list[tuple[str, str]] = []
    for name, value in _flatten(params):
        if value is None:
            continue
        out.append((name, "true" if value is True else "false" if value is False else str(value)))
    return out
