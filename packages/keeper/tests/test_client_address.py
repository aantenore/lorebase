"""Client address of a request: socket peer, trusted proxies and X-Forwarded-For."""

from __future__ import annotations

import ipaddress

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError

from lorebase_keeper.client_address import (
    ClientAddresses,
    client_address,
    in_networks,
    install,
    normalize_networks,
    parse_address,
)
from lorebase_keeper.settings import KeeperSettings

PROXIES = ClientAddresses(["10.0.0.0/8", "2001:db8:ffff::/48"])


def ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(text)


def test_an_untrusted_peer_is_the_client_whatever_it_forwards() -> None:
    assert PROXIES.resolve("203.0.113.9", ["198.51.100.1"]) == ip("203.0.113.9")
    assert ClientAddresses().resolve("10.0.0.1", ["198.51.100.1"]) == ip("10.0.0.1")


def test_a_trusted_peer_hands_over_to_the_last_forwarded_hop() -> None:
    assert PROXIES.resolve("10.0.0.1", ["198.51.100.1"]) == ip("198.51.100.1")


def test_a_trusted_peer_without_forwarding_is_the_client() -> None:
    assert PROXIES.resolve("10.0.0.1", []) == ip("10.0.0.1")
    assert PROXIES.resolve("10.0.0.1", [" "]) == ip("10.0.0.1")


def test_a_chain_of_trusted_proxies_is_walked_from_the_right() -> None:
    forwarded = ["198.51.100.1, 10.1.1.1, 10.2.2.2"]
    assert PROXIES.resolve("10.0.0.1", forwarded) == ip("198.51.100.1")


def test_entries_left_of_the_first_untrusted_hop_are_ignored() -> None:
    # The client wrote the first two entries itself; the proxy appended its real address.
    forwarded = ["10.9.9.9, 192.0.2.66, 203.0.113.50"]
    assert PROXIES.resolve("10.0.0.1", forwarded) == ip("203.0.113.50")


def test_several_header_lines_form_one_list_in_order() -> None:
    forwarded = ["192.0.2.66", "203.0.113.50, 10.1.1.1"]
    assert PROXIES.resolve("10.0.0.1", forwarded) == ip("203.0.113.50")


def test_a_chain_of_trusted_hops_only_yields_its_leftmost_hop() -> None:
    assert PROXIES.resolve("10.0.0.1", ["10.3.3.3, 10.2.2.2"]) == ip("10.3.3.3")


def test_ipv6_peers_and_hops() -> None:
    assert PROXIES.resolve("2001:db8:ffff::1", ["2001:db8:1::5"]) == ip("2001:db8:1::5")
    assert PROXIES.resolve("2001:db8:ffff::1", ["[2001:db8:1::5]:4711"]) == ip("2001:db8:1::5")
    assert PROXIES.resolve("2001:db8:1::5", ["192.0.2.1"]) == ip("2001:db8:1::5")


def test_ipv4_mapped_addresses_become_ipv4() -> None:
    assert PROXIES.resolve("::ffff:10.0.0.1", ["::ffff:192.0.2.1"]) == ip("192.0.2.1")


def test_a_malformed_hop_ends_the_walk_without_an_address() -> None:
    assert PROXIES.resolve("10.0.0.1", ["192.0.2.66, unknown"]) is None
    assert PROXIES.resolve("10.0.0.1", ["192.0.2.66, 10.1.1.1, not-an-ip"]) is None


def test_a_malformed_hop_left_of_the_client_is_never_read() -> None:
    assert PROXIES.resolve("10.0.0.1", ["garbage, 192.0.2.66"]) == ip("192.0.2.66")


def test_a_peer_that_is_not_an_address_is_unknown() -> None:
    assert PROXIES.resolve("testclient", ["192.0.2.66"]) is None
    assert PROXIES.resolve(None, ["192.0.2.66"]) is None


def test_addresses_with_ports_are_parsed() -> None:
    assert parse_address("192.0.2.1:8080") == ip("192.0.2.1")
    assert parse_address("[2001:db8::1]") == ip("2001:db8::1")
    assert parse_address("") is None


def test_networks_are_validated_and_normalized() -> None:
    assert normalize_networks(
        ["192.0.2.7/24", "2001:DB8::1/48", "198.51.100.1", "192.0.2.0/24"]
    ) == (
        "192.0.2.0/24",
        "2001:db8::/48",
        "198.51.100.1/32",
    )
    with pytest.raises(ValueError, match=r"10\.0\.0\.0/33"):
        normalize_networks(["10.0.0.0/33"])


def test_membership_ignores_entries_that_are_not_networks() -> None:
    assert in_networks(ip("192.0.2.5"), ["junk", "192.0.2.0/24"])
    assert not in_networks(ip("192.0.2.5"), ["junk"])
    assert not in_networks(None, ["0.0.0.0/0"])
    assert not in_networks(ip("2001:db8::1"), ["0.0.0.0/0"])


def test_trusted_proxies_setting_is_validated_and_normalized() -> None:
    settings = KeeperSettings(trusted_proxies=("10.1.2.3/8", "fd00::1/64"))
    assert settings.trusted_proxies == ("10.0.0.0/8", "fd00::/64")
    assert KeeperSettings().trusted_proxies == ()
    with pytest.raises(ValidationError, match="trusted_proxies"):
        KeeperSettings(trusted_proxies=("not-a-network",))


async def test_requests_resolve_through_the_application_resolver() -> None:
    app = FastAPI()
    install(app.state, ["10.0.0.0/8"])

    @app.get("/who")
    async def who(request: Request) -> dict[str, str | None]:
        found = client_address(request)
        return {"address": str(found) if found else None}

    async def ask(peer: str, **headers: str) -> object:
        transport = httpx.ASGITransport(app=app, client=(peer, 4000))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            return (await http.get("/who", headers=headers)).json()["address"]

    assert await ask("10.0.0.1", **{"X-Forwarded-For": "192.0.2.66"}) == "192.0.2.66"
    assert await ask("203.0.113.9", **{"X-Forwarded-For": "192.0.2.66"}) == "203.0.113.9"
