"""Keeper access for the tools: one typed Lorebase client per request.

Herald reaches Lorebase only through Keeper (AC-20), so authorization, masking, audit and rate
limits are enforced in one place. Every client carries the credential of the calling agent and
nothing else: no client is shared between requests, so no credential, connection or cached
answer can cross from one caller to another.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from lorebase_client import AsyncLorebaseClient, RetryPolicy
from lorebase_herald.settings import KeeperConnection
from lorebase_herald.tracing import call_headers, stamp_request


def bearer_token(credential: str | None) -> str | None:
    """The token of a credential: a forwarded `Bearer ...` value or a bare API key."""
    if credential is None:
        return None
    scheme, _, rest = credential.partition(" ")
    return rest.strip() if rest and scheme.lower() == "bearer" else credential


class KeeperClients:
    """Opens the client of one request. `transport` replaces the network (tests)."""

    def __init__(
        self, connection: KeeperConnection, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._connection = connection
        self._transport = transport

    def _transport_for_request(self) -> httpx.AsyncBaseTransport | None:
        if self._transport is None and not self._connection.verify_tls:
            return httpx.AsyncHTTPTransport(verify=False)
        return self._transport

    @asynccontextmanager
    async def open(self, credential: str | None) -> AsyncIterator[AsyncLorebaseClient]:
        """A client for one request, closed afterwards. With no credential, requests go out
        anonymous: the client reads no key from the environment. It carries the trace headers of
        the tool call being served; each of its requests gets a span of its own (see
        `lorebase_herald.tracing`)."""
        connection = self._connection
        async with AsyncLorebaseClient(
            connection.base_url,
            api_key=bearer_token(credential),
            api_prefix=connection.api_prefix,
            timeout=connection.timeout_s,
            retry=RetryPolicy(
                max_retries=connection.max_retries, backoff_max_s=connection.retry_max_wait_s
            ),
            headers={"User-Agent": connection.user_agent, **(call_headers.get() or {})},
            transport=self._transport_for_request(),
            event_hooks={"request": [stamp_request]},
            trust_env=connection.trust_env,
            env={},
        ) as client:
            yield client
