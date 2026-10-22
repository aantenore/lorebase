"""Document intake by URL: the guarded fetch behind `POST /domains/{d}/documents` with a URL.

The URL needs a scheme in `keeper.documents.url_schemes` and a host in `url_hosts` (exact
match). The host is then resolved once and every address must be global unicast, the check of
webhook targets and HTTP JSON Sources (`lorebase_store.webhooks.delivery.check_target`), unless
`url_allow_private_targets`. The request connects to those checked addresses only, never to a
new lookup, so a DNS answer that changes after the check (rebinding) cannot reach an internal
service; the `Host` header and the TLS server name keep the host name. Redirects are never
followed, proxies from the environment are ignored and the body is read in bounded steps.
"""

from __future__ import annotations

from typing import Any

import httpx

from lorebase_keeper.services.config import DocumentSettings
from lorebase_store.webhooks import WebhookSettings
from lorebase_store.webhooks.delivery import Resolver, check_target, resolve_host


class UrlRefusedError(ValueError):
    """The URL may not be fetched (scheme, host or the addresses it resolves to)."""


class UrlUnavailableError(RuntimeError):
    """The URL could not be fetched now (resolution, connection or an answer other than 200)."""


class DocumentTooLargeError(ValueError):
    """The body exceeds `keeper.documents.max_bytes`."""


def pin(url: httpx.URL, address: str) -> tuple[httpx.URL, dict[str, str], dict[str, Any]]:
    """`url` rewritten to connect to the checked `address`, with the `Host` header and request
    extensions that keep the host name for the server and for TLS (as Scribe's webhook sender
    and Source connectors do)."""
    return (
        url.copy_with(host=address.split("%", 1)[0]),
        {"Host": url.netloc.decode("ascii")},
        {"sni_hostname": url.host} if url.scheme == "https" else {},
    )


async def checked_addresses(
    url: httpx.URL, settings: DocumentSettings, resolve: Resolver
) -> tuple[str, ...]:
    """Addresses a fetch of `url` may connect to; empty when private targets are allowed (the
    client then resolves the host itself)."""
    if url.scheme not in settings.url_schemes:
        raise UrlRefusedError(f"scheme must be one of {', '.join(settings.url_schemes)}")
    allowed = {host.lower() for host in settings.url_hosts}
    if not url.host or url.host.lower() not in allowed:
        raise UrlRefusedError("the host is not in keeper.documents.url_hosts")
    checks = WebhookSettings(
        allowed_schemes=settings.url_schemes,
        allow_private_targets=settings.url_allow_private_targets,
    )
    target = await check_target(str(url), checks, resolve)
    if target.problem is None:
        return target.addresses
    if target.problem.startswith("cannot resolve"):
        raise UrlUnavailableError("the URL host cannot be resolved")
    # `check_target` problems never contain the host name or its addresses.
    raise UrlRefusedError(
        f"the URL host {target.problem}, refused unless keeper.documents.url_allow_private_targets"
    )


async def fetch(
    url: str,
    settings: DocumentSettings,
    *,
    resolve: Resolver = resolve_host,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """The body of a 200 answer to GET `url`, at most `settings.max_bytes` long."""
    target = httpx.URL(url)
    addresses = await checked_addresses(target, settings, resolve)
    attempts = [pin(target, address) for address in addresses] or [(target, {}, {})]
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=settings.url_timeout_s,
        trust_env=False,
        transport=transport,
    ) as client:
        failure: httpx.HTTPError | None = None
        for pinned, headers, extensions in attempts:
            try:
                return await _get(client, pinned, headers, extensions, settings.max_bytes)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                failure = error  # the next checked address, never a new lookup
            except httpx.HTTPError as error:
                raise UrlUnavailableError(f"the URL could not be fetched: {error}") from None
    raise UrlUnavailableError(f"the URL could not be fetched: {failure}")


async def _get(
    client: httpx.AsyncClient,
    url: httpx.URL,
    headers: dict[str, str],
    extensions: dict[str, Any],
    limit: int,
) -> bytes:
    data = bytearray()
    async with client.stream("GET", url, headers=headers, extensions=extensions) as response:
        if response.status_code != 200:
            raise UrlUnavailableError(f"the URL answered {response.status_code}")
        async for block in response.aiter_bytes():
            data.extend(block)
            if len(data) > limit:
                raise DocumentTooLargeError(f"the document exceeds {limit} bytes")
    return bytes(data)
