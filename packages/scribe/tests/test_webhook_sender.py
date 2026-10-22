"""The webhook sender connects to the checked addresses only, never follows redirects, and ends
every attempt within its budget whatever the receiver does."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpcore
import httpx
import pytest

from lorebase_scribe.webhooks import HttpxSender


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


class EndlessBody(httpx.AsyncByteStream):
    """A response body that never ends, one small chunk at a time."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.01)
            yield b"x" * 1024


class Stalling(httpx.AsyncBaseTransport):
    """Answers 200 with an endless body, or waits before answering at all."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.delay_s)
        return httpx.Response(200, stream=EndlessBody())


async def test_the_response_body_is_never_read() -> None:
    # A target streaming an endless body must not hold the worker nor grow its memory.
    async with httpx.AsyncClient(transport=Stalling()) as client:
        status = await asyncio.wait_for(
            HttpxSender(client)("https://hooks.example.com/x", b"{}", {}, 1.0, []), 3
        )
    assert status == 200


async def test_the_timeout_bounds_the_whole_request() -> None:
    # httpx timeouts apply to each read, so a target answering slowly could hold the sender
    # far beyond `timeout_s`; the sender bounds the whole exchange itself.
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with httpx.AsyncClient(transport=Stalling(delay_s=30)) as client:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                HttpxSender(client)("https://hooks.example.com/x", b"{}", {}, 0.2, []), 5
            )
    assert loop.time() - started < 2


# Receivers that are gone or misbehave: each attempt must end within its budget.


class Blackhole(httpcore.AsyncMockBackend):
    """Connections to `dropped` never complete, as when every SYN is dropped; other addresses
    answer 204. The connect timeout is applied the way httpcore's own backend applies it."""

    def __init__(self, dropped: frozenset[str]) -> None:
        super().__init__([b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"])
        self.dropped = dropped
        self.connects: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore's backend interface)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append(host)
        if host in self.dropped:
            try:
                with anyio.fail_after(timeout):
                    await anyio.sleep_forever()
            except TimeoutError:
                raise httpcore.ConnectTimeout("dropped") from None
        return await super().connect_tcp(host, port, timeout, local_address, socket_options)


def blackhole_client(backend: Blackhole) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(network_backend=backend)
    return httpx.AsyncClient(transport=transport)


async def test_a_dropped_address_gives_way_to_the_next_after_the_connect_timeout() -> None:
    backend = Blackhole(frozenset({"192.0.2.1"}))
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with blackhole_client(backend) as client:
        status = await HttpxSender(client, connect_timeout_s=0.2)(
            "http://hooks.example.com/x", b"{}", {}, 5.0, ["192.0.2.1", "93.184.215.14"]
        )
    assert status == 204
    assert backend.connects == ["192.0.2.1", "93.184.215.14"]
    assert loop.time() - started < 2


async def test_a_target_dropping_every_packet_fails_within_the_budget() -> None:
    backend = Blackhole(frozenset({"192.0.2.1", "192.0.2.2"}))
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with blackhole_client(backend) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await HttpxSender(client, connect_timeout_s=0.2)(
                "http://hooks.example.com/x", b"{}", {}, 5.0, ["192.0.2.1", "192.0.2.2"]
            )
    assert backend.connects == ["192.0.2.1", "192.0.2.2"]
    assert loop.time() - started < 2


def test_every_phase_of_a_request_has_a_timeout() -> None:
    client = httpx.AsyncClient()
    assert HttpxSender(client, connect_timeout_s=3.0)._timeout(10.0) == httpx.Timeout(
        10.0, connect=3.0
    )
    # the connect phase never outlasts the attempt
    assert HttpxSender(client, connect_timeout_s=3.0)._timeout(1.0) == httpx.Timeout(1.0)
    assert HttpxSender(client)._timeout(4.0) == httpx.Timeout(4.0)


Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@asynccontextmanager
async def tcp_server(handle: Handler) -> AsyncIterator[int]:
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Accepts the connection and never answers."""
    await reader.read(65536)
    await asyncio.sleep(3600)


def closes_after(answer: bytes) -> Handler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        writer.write(answer)
        await writer.drain()
        writer.close()

    return handle


async def send_to(port: int, timeout_s: float = 0.5) -> int:
    async with httpx.AsyncClient(trust_env=False) as client:
        sender = HttpxSender(client, connect_timeout_s=timeout_s)
        return await asyncio.wait_for(
            sender(f"http://127.0.0.1:{port}/hook", b"{}", {}, timeout_s, []), 5
        )


@pytest.mark.integration
async def test_a_receiver_that_accepts_and_never_answers_times_out() -> None:
    loop = asyncio.get_running_loop()
    async with tcp_server(silent) as port:
        started = loop.time()
        with pytest.raises(TimeoutError):
            await send_to(port)
        assert loop.time() - started < 2


@pytest.mark.integration
async def test_a_receiver_that_is_gone_fails_at_once() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens there
    with pytest.raises(httpx.ConnectError):
        await send_to(port)


@pytest.mark.integration
async def test_a_receiver_closing_midway_fails_or_answers_without_waiting() -> None:
    async with tcp_server(closes_after(b"HTTP/1.1 200 O")) as port:
        with pytest.raises(httpx.RemoteProtocolError):
            await send_to(port)
    # Once the status line is in, the body is never read: a cut body does not matter.
    cut_body = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nabc"
    async with tcp_server(closes_after(cut_body)) as port:
        assert await send_to(port) == 200
