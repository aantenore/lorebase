"""The webhook sender connects to the checked addresses only and never follows redirects."""

from __future__ import annotations

import httpx
import pytest

from lorebase_keeper.services.webhooks import HttpxSender


class Transport(httpx.AsyncBaseTransport):
    """Records requests; refuses connections to `unreachable`, redirects everything else."""

    def __init__(self, unreachable: frozenset[str] = frozenset()) -> None:
        self.requests: list[httpx.Request] = []
        self.unreachable = unreachable

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.unreachable:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest"})


async def test_request_goes_to_the_checked_address_with_the_host_name_kept() -> None:
    transport = Transport()
    async with httpx.AsyncClient(transport=transport) as client:
        status = await HttpxSender(client)(
            "https://hooks.example.com:8443/x?y=1",
            b"{}",
            {"Content-Type": "application/json"},
            5.0,
            ["93.184.215.14"],
        )
    assert status == 302  # answered as is: the redirect is not followed
    (request,) = transport.requests
    assert request.url.host == "93.184.215.14"
    assert request.url.port == 8443
    assert request.url.raw_path == b"/x?y=1"
    assert request.headers["Host"] == "hooks.example.com:8443"
    assert request.extensions["sni_hostname"] == "hooks.example.com"


async def test_next_checked_address_is_tried_without_a_new_lookup() -> None:
    transport = Transport(unreachable=frozenset({"2001:db8::1"}))
    async with httpx.AsyncClient(transport=transport) as client:
        status = await HttpxSender(client)(
            "https://hooks.example.com/x", b"{}", {}, 5.0, ["2001:db8::1", "93.184.215.14"]
        )
    assert status == 302
    assert [r.url.host for r in transport.requests] == ["2001:db8::1", "93.184.215.14"]

    unreachable = Transport(unreachable=frozenset({"93.184.215.14"}))
    async with httpx.AsyncClient(transport=unreachable) as client:
        with pytest.raises(httpx.ConnectError):
            await HttpxSender(client)(
                "https://hooks.example.com/x", b"{}", {}, 5.0, ["93.184.215.14"]
            )


async def test_without_checked_addresses_the_url_is_used_as_given() -> None:
    transport = Transport()
    async with httpx.AsyncClient(transport=transport) as client:
        await HttpxSender(client)("http://127.0.0.1:8080/hook", b"{}", {}, 5.0, [])
    (request,) = transport.requests
    assert str(request.url) == "http://127.0.0.1:8080/hook"
    assert "sni_hostname" not in request.extensions
