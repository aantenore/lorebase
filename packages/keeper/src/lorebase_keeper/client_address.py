"""The network address of the client behind a request.

The client is the socket peer, unless the peer is one of `keeper.trusted_proxies`. Then
`X-Forwarded-For` is read from right to left: each hop that is itself a trusted proxy is
skipped and the first other address is the client. Entries further left were written by the
client and are never believed. A hop that is not an IP address (a proxy writing `unknown`, a
garbled header) ends the walk with no address rather than trusting what lies beyond it.

Keeper serves with the proxy handling of the ASGI server turned off, so this module is the only
place that reads forwarding headers; API key allowlists and failed authentication limits both
use it.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from typing import Any, cast

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

FORWARDED_FOR = "x-forwarded-for"
_STATE_KEY = "client_address"
_APP_STATE_KEY = "client_addresses"


def parse_network(text: str) -> IPNetwork:
    """A network from `10.0.0.0/8`, `2001:db8::/32` or a single address; host bits are cleared.

    Raises `ValueError` when `text` is not one.
    """
    return ipaddress.ip_network(text.strip(), strict=False)


def normalize_networks(entries: Iterable[str]) -> tuple[str, ...]:
    """Validated networks in canonical form, duplicates dropped, order kept."""
    return tuple(dict.fromkeys(str(parse_network(entry)) for entry in entries))


def parse_address(text: str) -> IPAddress | None:
    """An address from a socket peer or a forwarded hop; None when `text` is not one.

    Brackets and a port (`[2001:db8::1]:443`, `192.0.2.1:8080`) are accepted, and IPv4
    addresses mapped into IPv6 become plain IPv4 addresses.
    """
    candidate = text.strip()
    if candidate.startswith("["):
        candidate = candidate[1:].partition("]")[0]
    elif candidate.count(":") == 1:
        candidate = candidate.partition(":")[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def in_networks(address: IPAddress | None, networks: Sequence[str]) -> bool:
    """Whether `address` lies in one of `networks`. Entries that are not networks match nothing;
    an unknown address matches nothing."""
    if address is None:
        return False
    for entry in networks:
        try:
            network = parse_network(entry)
        except ValueError:
            continue
        if address in network:
            return True
    return False


class ClientAddresses:
    """Resolves the client address of requests for a list of trusted proxy networks."""

    def __init__(self, trusted_proxies: Iterable[str] = ()) -> None:
        self._trusted = tuple(parse_network(entry) for entry in trusted_proxies)

    def trusted(self, address: IPAddress) -> bool:
        """Whether `address` is a trusted proxy."""
        return any(address in network for network in self._trusted)

    def resolve(self, peer: str | None, forwarded_for: Sequence[str] = ()) -> IPAddress | None:
        """The client address from the socket peer and the `X-Forwarded-For` header values (in
        the order received); None when it cannot be told."""
        address = parse_address(peer) if peer else None
        if address is None or not self.trusted(address):
            return address
        hops = [hop for value in forwarded_for for hop in value.split(",")]
        for hop in reversed(hops):
            if not hop.strip():
                continue
            address = parse_address(hop)
            if address is None:
                return None
            if not self.trusted(address):
                return address
        return address

    def of(self, connection: HTTPConnection) -> IPAddress | None:
        """The client address of a request, computed once and kept in the request state."""
        state = connection.scope.setdefault("state", {})
        if _STATE_KEY not in state:
            client = connection.scope.get("client")
            peer = str(client[0]) if client else None
            forwarded = Headers(scope=connection.scope).getlist(FORWARDED_FOR)
            state[_STATE_KEY] = self.resolve(peer, forwarded)
        return cast("IPAddress | None", state[_STATE_KEY])


_UNTRUSTING = ClientAddresses()


def install(app_state: Any, trusted_proxies: Iterable[str]) -> None:
    """Attach the resolver for `trusted_proxies` to an application's state."""
    setattr(app_state, _APP_STATE_KEY, ClientAddresses(trusted_proxies))


def client_address(connection: HTTPConnection) -> IPAddress | None:
    """The client address of a request, by the application's trusted proxies (none when the
    application has no resolver: the socket peer is the client)."""
    resolver = getattr(connection.app.state, _APP_STATE_KEY, None)
    return (resolver if isinstance(resolver, ClientAddresses) else _UNTRUSTING).of(connection)
