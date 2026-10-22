"""Conditional GET and response caching of reads.

Every GET read answers with `ETag` = the Revision it was served from plus a digest of the path
and the canonical query parameters, `Cache-Control: private, must-revalidate` and the
`Lorebase-Revision` header; `If-None-Match` with that tag answers 304. Responses are cached
behind `ResponseCache`, keyed by `(Domain, Revision, path, canonical query)`. Revisions are
immutable, so a commit never invalidates an entry: it moves the branch to a new Revision and
therefore to new keys. The cache is consulted after the read is resolved and authorized, so it
never serves a caller something it could not read. Bodies are masked for the caller's
clearance, which is part of the key (`variant`), and hold vault references, not vault values:
`disclose` replaces them on every answer, cached or not (`reading.sensitivity`).

Only the in-process `InMemoryResponseCache` exists; a shared adapter (for example over Valkey)
implements the same protocol.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol

from fastapi import Request, Response
from opentelemetry import metrics
from pydantic import BaseModel

from lorebase_keeper.auth.read_audit import note_all, noted_sensitive
from lorebase_keeper.settings import ReadCacheSettings

JSON = "application/json"

Disclose = Callable[[bytes, str], Awaitable[bytes]]
"""Rewrites a body of a media type as it is sent (vault references, `reading.sensitivity`)."""
CACHE_CONTROL = "private, must-revalidate"

_LOOKUPS = metrics.get_meter("lorebase.keeper").create_counter(
    "lorebase.keeper.cache.requests",
    unit="{request}",
    description="Read response cache lookups, by result (hit, miss, bypass when disabled).",
)


@dataclass(frozen=True, slots=True)
class CacheKey:
    domain_id: str
    revision: str
    path: str
    query: tuple[tuple[str, str], ...]
    variant: str = ""
    """What else the body depends on, such as the Kinds the caller may read."""


@dataclass(frozen=True, slots=True)
class CachedResponse:
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    time_travel: bool = False
    media_type: str = JSON
    sensitive: frozenset[str] = frozenset()
    """Sensitive values the first rendering showed or masked (`read_audit.note_sensitive`),
    reported again by every answer from the cache."""


class ResponseCache(Protocol):
    """Storage of read responses (in process now; a shared store later)."""

    async def get(self, key: CacheKey) -> CachedResponse | None: ...

    async def put(self, key: CacheKey, value: CachedResponse) -> None: ...


class InMemoryResponseCache:
    """LRU bounded by entries and bytes, with a time to live and a cap on time travel entries."""

    def __init__(
        self,
        settings: ReadCacheSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._entries: OrderedDict[CacheKey, tuple[float, CachedResponse]] = OrderedDict()
        self._bytes = 0
        self._time_travel = 0
        self._lock = Lock()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def size_bytes(self) -> int:
        return self._bytes

    async def get(self, key: CacheKey) -> CachedResponse | None:
        with self._lock:
            found = self._entries.get(key)
            if found is None:
                return None
            expires, value = found
            if expires <= self._clock():
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return value

    async def put(self, key: CacheKey, value: CachedResponse) -> None:
        settings = self._settings
        size = len(value.body)
        if size > settings.max_body_bytes or size > settings.max_bytes:
            return
        with self._lock:
            if key in self._entries:
                self._remove(key)
            if value.time_travel and self._time_travel >= settings.time_travel_max_entries:
                return
            self._entries[key] = (self._clock() + settings.ttl_s, value)
            self._bytes += size
            self._time_travel += value.time_travel
            while len(self._entries) > settings.max_entries or self._bytes > settings.max_bytes:
                self._remove(next(iter(self._entries)))

    def _remove(self, key: CacheKey) -> None:
        _, value = self._entries.pop(key)
        self._bytes -= len(value.body)
        self._time_travel -= value.time_travel


def build_cache(settings: ReadCacheSettings) -> ResponseCache | None:
    return InMemoryResponseCache(settings) if settings.enabled else None


def cache_key(request: Request, domain_id: str, revision: str, variant: str = "") -> CacheKey:
    query = tuple(sorted(request.query_params.multi_items()))
    return CacheKey(domain_id, revision, request.url.path, query, variant)


def entity_tag(key: CacheKey) -> str:
    """Strong ETag: the Revision plus a digest of the path and canonical query parameters."""
    digest = hashlib.sha256(key.path.encode())
    for name, value in key.query:
        digest.update(b"\x00" + name.encode() + b"\x1f" + value.encode())
    if key.variant:
        digest.update(b"\x1e" + key.variant.encode())
    return f'"{key.revision}.{digest.hexdigest()[:16]}"'


def _matches(request: Request, tag: str) -> bool:
    header = request.headers.get("if-none-match")
    if header is None:
        return False
    candidates = {t.strip() for t in header.split(",")}
    return "*" in candidates or tag in candidates or f"W/{tag}" in candidates


def not_modified(
    request: Request,
    *,
    domain_id: str,
    revision: str,
    headers: Mapping[str, str] | None = None,
    variant: str = "",
) -> Response | None:
    """The 304 answer of a GET read whose `If-None-Match` names its current tag, else `None`."""
    tag = entity_tag(cache_key(request, domain_id, revision, variant))
    if not _matches(request, tag):
        return None
    common = {"ETag": tag, "Cache-Control": CACHE_CONTROL, **(headers or {})}
    return Response(status_code=304, headers=common)


async def respond(
    request: Request,
    cache: ResponseCache | None,
    *,
    domain_id: str,
    revision: str,
    produce: Callable[[], Awaitable[BaseModel]],
    time_travel: bool = False,
    headers: Mapping[str, str] | None = None,
    variant: str = "",
    disclose: Disclose | None = None,
) -> Response:
    """Answer a GET read: 304, a cached body, or `produce()` serialized and cached.

    `revision` names everything the response depends on besides the request (one Revision id,
    or several joined for a diff); `variant` anything else it depends on, such as the Kinds
    the caller may read and its clearance; `headers` are added to every answer; `disclose`
    post-processes every body sent (not the cached one).
    """

    async def body() -> tuple[bytes, str]:
        return (await produce()).model_dump_json().encode(), JSON

    return await respond_with(
        request,
        cache,
        cache_key(request, domain_id, revision, variant),
        body,
        time_travel=time_travel,
        headers=headers,
        disclose=disclose,
    )


async def respond_with(
    request: Request,
    cache: ResponseCache | None,
    key: CacheKey,
    produce: Callable[[], Awaitable[tuple[bytes, str]]],
    *,
    time_travel: bool = False,
    headers: Mapping[str, str] | None = None,
    disclose: Disclose | None = None,
) -> Response:
    """Answer a read under `key`: 304, a cached body, or `produce()` (body and media type),
    cached. The ETag is `entity_tag(key)`. `disclose`, when given, rewrites bodies as they are
    sent."""
    tag = entity_tag(key)
    common = {"ETag": tag, "Cache-Control": CACHE_CONTROL, **(headers or {})}
    if _matches(request, tag):
        return Response(status_code=304, headers=common)
    cached = None if cache is None else await cache.get(key)
    result = "bypass" if cache is None else "miss" if cached is None else "hit"
    _LOOKUPS.add(1, {"result": result})
    if cached is not None:
        note_all(cached.sensitive)
        return Response(
            await _sent(cached.body, cached.media_type, disclose),
            media_type=cached.media_type,
            headers={**cached.headers, **common},
        )
    body, media_type = await produce()
    if cache is not None:
        await cache.put(
            key,
            CachedResponse(body, dict(headers or {}), time_travel, media_type, noted_sensitive()),
        )
    return Response(await _sent(body, media_type, disclose), media_type=media_type, headers=common)


async def _sent(body: bytes, media_type: str, disclose: Disclose | None) -> bytes:
    return body if disclose is None else await disclose(body, media_type)
