"""HTTP sender of webhook deliveries, for `lorebase_store.webhooks.deliver_pending`.

The store makes no HTTP calls itself; Scribe's `webhook_delivery` jobs pass this `Sender`. It
uses an `httpx.AsyncClient` owned by the caller (build it with `trust_env=False`, so no proxy from
the environment sits between the checks and the target) and:

- connects to the addresses the worker checked, never to a fresh lookup of the host: the URL
  is rewritten to the address while the `Host` header and the TLS server name (SNI and
  certificate verification) keep the host name, so a DNS answer that changed after the check
  cannot reach an internal service;
- never follows redirects (a redirect could lead to a target the checks did not see);
- never reads the response body, and bounds each attempt by the timeout as a whole, so a
  target that streams or trickles cannot hold the worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import httpx


def pin(url: httpx.URL, address: str) -> tuple[httpx.URL, dict[str, str], dict[str, Any]]:
    """`url` rewritten to connect to the checked `address`, with the `Host` header and request
    extensions that keep the host name for the server and for TLS (SNI and certificate
    verification)."""
    return (
        url.copy_with(host=address.split("%", 1)[0]),
        {"Host": url.netloc.decode("ascii")},
        {"sni_hostname": url.host} if url.scheme == "https" else {},
    )


class HttpxSender:
    """`Sender` over an `httpx.AsyncClient`."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        target = httpx.URL(url)
        if not addresses:
            return await self._post(target, body, dict(headers), timeout_s, {})
        failure: httpx.TransportError | None = None
        for address in addresses:
            pinned, host, extensions = pin(target, address)
            try:
                return await self._post(pinned, body, {**headers, **host}, timeout_s, extensions)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                failure = error  # the next checked address, never a new lookup
        assert failure is not None
        raise failure

    async def _post(
        self,
        url: httpx.URL,
        body: bytes,
        headers: dict[str, str],
        timeout_s: float,
        extensions: dict[str, Any],
    ) -> int:
        # httpx applies `timeout` to each connect, write and read, not to the whole exchange,
        # and `post` reads the whole body: bound the exchange and close without reading it.
        async with (
            asyncio.timeout(timeout_s),
            self._client.stream(
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=timeout_s,
                follow_redirects=False,
                extensions=extensions,
            ) as response,
        ):
            return response.status_code
