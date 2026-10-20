"""HTTP sender of webhook deliveries, for `lorebase_store.webhooks.deliver_pending`.

The store worker makes no HTTP calls itself; whoever runs it passes a `Sender`. This one uses
an `httpx.AsyncClient` owned by the caller and never follows redirects (a redirect could lead
to a target the URL checks did not see).
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx


class HttpxSender:
    """`Sender` over an `httpx.AsyncClient`."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout_s: float
    ) -> int:
        response = await self._client.post(
            url, content=body, headers=dict(headers), timeout=timeout_s, follow_redirects=False
        )
        return response.status_code
