"""Keeper access: one typed client per request, carrying only the caller's credential.

No client is shared between requests, so no credential, connection or cached answer crosses
from one caller to another. Every request of an exploration also carries the agent trace
headers: the session id for the whole exploration and the name of the tool making the call,
set per call through `tool_scope`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

import httpx

from lorebase_client import AsyncLorebaseClient, RetryPolicy
from lorebase_guide.auth import Caller
from lorebase_guide.settings import GuideSettings

_REQUEST_HEADERS: ContextVar[Mapping[str, str] | None] = ContextVar(
    "guide_request_headers", default=None
)


@contextmanager
def tool_scope(headers: Mapping[str, str]) -> Iterator[None]:
    """Add `headers` to every Keeper request made inside the block (same task)."""
    token = _REQUEST_HEADERS.set(headers)
    try:
        yield
    finally:
        _REQUEST_HEADERS.reset(token)


class _HeaderTransport(httpx.AsyncBaseTransport):
    """Adds the headers of the current `tool_scope` to each request."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for name, value in (_REQUEST_HEADERS.get() or {}).items():
            request.headers[name] = value
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class KeeperClients:
    """Opens the Keeper client of one request. `transport` replaces the network (tests)."""

    def __init__(
        self, settings: GuideSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def probe(self, timeout_s: float) -> bool:
        """Whether Keeper answers its liveness route (no credential involved)."""
        keeper = self._settings.keeper
        transport = self._transport or httpx.AsyncHTTPTransport(verify=keeper.verify_tls)
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout_s, trust_env=False
        ) as http:
            try:
                response = await http.get(f"{keeper.base_url}/healthz")
            except httpx.HTTPError:
                return False
        return response.status_code == 200

    @asynccontextmanager
    async def open(
        self, caller: Caller, headers: Mapping[str, str] | None = None
    ) -> AsyncIterator[AsyncLorebaseClient]:
        """A client sending the caller's credential and `headers`, closed afterwards. The
        client reads nothing from the environment: no key can be picked up by accident."""
        keeper = self._settings.keeper
        inner = self._transport or httpx.AsyncHTTPTransport(verify=keeper.verify_tls)
        async with AsyncLorebaseClient(
            keeper.base_url,
            api_prefix=keeper.api_prefix,
            timeout=keeper.timeout_s,
            retry=RetryPolicy(
                max_retries=keeper.max_retries, backoff_max_s=keeper.retry_max_wait_s
            ),
            headers={
                "User-Agent": keeper.user_agent,
                **(headers or {}),
                **caller.keeper_headers(self._settings.http),
            },
            transport=_HeaderTransport(inner),
            trust_env=False,
            env={},
        ) as client:
            yield client
