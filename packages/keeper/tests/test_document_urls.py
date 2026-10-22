"""Document intake by URL: allowlisted hosts, global addresses only, pinned connections."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest

from lorebase_keeper.services.config import DocumentSettings
from lorebase_keeper.services.document_urls import (
    DocumentTooLargeError,
    UrlRefusedError,
    UrlUnavailableError,
    fetch,
)

HOST = "docs.example.org"
URL = f"https://{HOST}/guides/a.md"
PUBLIC = "93.184.215.14"
OTHER_PUBLIC = "93.184.215.15"


def settings(**overrides: object) -> DocumentSettings:
    return DocumentSettings.model_validate({"url_hosts": [HOST], **overrides})


class FakeDns:
    """A resolver answering fixed addresses and recording the hosts it was asked for."""

    def __init__(self, *addresses: str) -> None:
        self.addresses = addresses
        self.calls: list[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.calls.append(host)
        return self.addresses


class Recorder:
    """A transport handler answering `status` with `body` and recording every request."""

    def __init__(self, body: bytes = b"# Guide\n", status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.body = body
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, content=self.body)


async def test_connects_to_the_checked_address_and_keeps_the_host_name() -> None:
    recorder, dns = Recorder(), FakeDns(PUBLIC)
    data = await fetch(URL, settings(), resolve=dns, transport=httpx.MockTransport(recorder))
    assert data == b"# Guide\n"
    (request,) = recorder.requests
    assert request.url.host == PUBLIC
    assert request.url.path == "/guides/a.md"
    assert request.headers["Host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    assert dns.calls == [HOST]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.10",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_private_loopback_and_link_local_addresses_are_refused(address: str) -> None:
    recorder = Recorder()
    with pytest.raises(UrlRefusedError, match="private address"):
        await fetch(
            URL,
            settings(),
            resolve=FakeDns(PUBLIC, address),
            transport=httpx.MockTransport(recorder),
        )
    assert recorder.requests == []


async def test_private_targets_can_be_allowed_without_pinning() -> None:
    recorder, dns = Recorder(), FakeDns("10.0.0.5")
    await fetch(
        URL,
        settings(url_allow_private_targets=True),
        resolve=dns,
        transport=httpx.MockTransport(recorder),
    )
    assert recorder.requests[0].url.host == HOST
    assert dns.calls == []


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (f"http://{HOST}/a.md", "scheme"),
        ("https://elsewhere.example.org/a.md", "url_hosts"),
        ("https://127.0.0.1/a.md", "url_hosts"),
    ],
)
async def test_scheme_and_host_must_be_allowed(url: str, message: str) -> None:
    dns = FakeDns(PUBLIC)
    with pytest.raises(UrlRefusedError, match=message):
        await fetch(url, settings(), resolve=dns)
    assert dns.calls == []


async def test_unresolvable_hosts_and_other_answers_are_unavailable() -> None:
    async def fails(host: str) -> Sequence[str]:
        raise OSError("no such host")

    with pytest.raises(UrlUnavailableError, match="cannot be resolved"):
        await fetch(URL, settings(), resolve=fails)
    with pytest.raises(UrlUnavailableError, match="answered 302"):
        await fetch(
            URL,
            settings(),
            resolve=FakeDns(PUBLIC),
            transport=httpx.MockTransport(Recorder(status=302)),
        )


async def test_the_next_checked_address_is_tried_on_connection_failure() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == PUBLIC:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, content=b"ok")

    data = await fetch(
        URL,
        settings(),
        resolve=FakeDns(PUBLIC, OTHER_PUBLIC),
        transport=httpx.MockTransport(handler),
    )
    assert data == b"ok"
    assert seen == [PUBLIC, OTHER_PUBLIC]


async def test_bodies_above_max_bytes_are_refused() -> None:
    with pytest.raises(DocumentTooLargeError):
        await fetch(
            URL,
            settings(max_bytes=4),
            resolve=FakeDns(PUBLIC),
            transport=httpx.MockTransport(Recorder(body=b"too long")),
        )
