from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from lorebase_inference.errors import EndpointError
from lorebase_inference.settings import RetrySettings
from lorebase_inference.transport import EndpointClient, parse_retry_after


def make_client(endpoint, *, key: str | None = None, **retry: float) -> EndpointClient:
    return EndpointClient(
        base_url="http://llm.test/v1/",
        api_key=SecretStr(key) if key else None,
        timeout_s=5,
        retry=RetrySettings(**{"max_retries": 3, "backoff_base_s": 1, "backoff_max_s": 8, **retry}),
        transport=endpoint.transport,
        sleep=endpoint.sleep,
        jitter=lambda: 1.0,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("7", 7.0), ("0", 0.0), ("1.5", 1.5), ("-3", 0.0), ("", None), ("soon", None), (None, None)],
)
def test_retry_after_seconds(value, expected) -> None:
    assert parse_retry_after(value) == expected


def test_retry_after_http_date_is_relative_to_now() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Thu, 01 Jan 2026 12:00:30 GMT", now=lambda: now) == 30
    assert parse_retry_after("Thu, 01 Jan 2026 11:00:00 GMT", now=lambda: now) == 0


async def test_post_sends_json_to_the_joined_url_with_bearer_key(scripted) -> None:
    endpoint = scripted(httpx.Response(200, json={"ok": 1}))
    async with make_client(endpoint, key="sk-test") as client:
        assert await client.post("/chat/completions", {"a": 1}) == {"ok": 1}

    request = endpoint.requests[0]
    assert str(request.url) == "http://llm.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert endpoint.bodies() == [{"a": 1}]


async def test_no_authorization_header_without_a_key(scripted) -> None:
    endpoint = scripted(httpx.Response(200, json={}))
    async with make_client(endpoint) as client:
        await client.post("/x", {})
    assert "authorization" not in endpoint.requests[0].headers


async def test_429_is_retried_honoring_retry_after(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(429, "slow down", **{"Retry-After": "3"}),
        httpx.Response(200, json={"done": True}),
    )
    async with make_client(endpoint) as client:
        assert await client.post("/x", {}) == {"done": True}
    assert endpoint.sleeps == [3.0]
    assert len(endpoint.requests) == 2


async def test_retry_after_is_capped_by_backoff_max(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(429, "wait", **{"Retry-After": "600"}), httpx.Response(200, json={})
    )
    async with make_client(endpoint, backoff_max_s=8) as client:
        await client.post("/x", {})
    assert endpoint.sleeps == [8.0]


async def test_server_errors_back_off_exponentially_then_give_up(scripted) -> None:
    endpoint = scripted(scripted.api_error(503, "overloaded"))
    async with make_client(endpoint) as client:
        with pytest.raises(EndpointError, match="HTTP 503: overloaded") as caught:
            await client.post("/x", {})
    assert caught.value.status == 503
    assert endpoint.sleeps == [1.0, 2.0, 4.0]
    assert len(endpoint.requests) == 4


async def test_client_errors_are_not_retried(scripted) -> None:
    endpoint = scripted(scripted.api_error(401, "bad key"))
    async with make_client(endpoint) as client:
        with pytest.raises(EndpointError, match="HTTP 401") as caught:
            await client.post("/x", {})
    assert caught.value.status == 401
    assert endpoint.sleeps == []
    assert len(endpoint.requests) == 1


async def test_connection_errors_are_retried(scripted) -> None:
    endpoint = scripted(
        httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(200, json={"a": 1})
    )
    async with make_client(endpoint) as client:
        assert await client.post("/x", {}) == {"a": 1}
    assert len(endpoint.sleeps) == 2


async def test_connection_errors_surface_after_the_retries(scripted) -> None:
    endpoint = scripted(httpx.ConnectError("refused"))
    async with make_client(endpoint, max_retries=1) as client:
        with pytest.raises(EndpointError, match="request failed \\(ConnectError\\)"):
            await client.post("/x", {})
    assert len(endpoint.requests) == 2


async def test_non_json_answer_is_an_endpoint_error(scripted) -> None:
    endpoint = scripted(httpx.Response(200, text="<html>gateway</html>"))
    async with make_client(endpoint) as client:
        with pytest.raises(EndpointError, match="not JSON"):
            await client.post("/x", {})


def test_base_url_must_be_http() -> None:
    with pytest.raises(ValueError, match="http"):
        EndpointClient(base_url="localhost:11434", api_key=None, timeout_s=1, retry=RetrySettings())


@pytest.mark.parametrize("status", [401, 403])
async def test_credential_errors_carry_no_server_text(scripted, status) -> None:
    endpoint = scripted(
        scripted.api_error(status, "Incorrect API key provided: sk-proj-****wxyz; account Acme")
    )
    async with make_client(endpoint, key="sk-proj-real-key-wxyz") as client:
        with pytest.raises(EndpointError, match=f"HTTP {status}") as caught:
            await client.post("/x", {})
    assert caught.value.status == status
    assert caught.value.detail == ""
    for leaked in ("Incorrect", "sk-proj", "wxyz", "Acme"):
        assert leaked not in str(caught.value)


async def test_credentials_in_error_text_are_scrubbed(scripted) -> None:
    configured = "custom.Configured-Key!42"
    base64_run = "QWxhZGRpbjpvcGVuIHNlc2FtZQ0K3hYp9Zt1Lk7wRb2NcVa8"
    text = (
        f"bad request for key {configured}: header Authorization: Bearer abc.def-ghi, "
        f"other key sk-live_0123456789abcdef, masked sk-****1234, token {base64_run}, "
        "hex 0123456789abcdef0123456789abcdef01; response_format is not supported"
    )
    endpoint = scripted(scripted.api_error(400, text))
    async with make_client(endpoint, key=configured) as client:
        with pytest.raises(EndpointError, match="HTTP 400") as caught:
            await client.post("/x", {})
    message = str(caught.value)
    for leaked in (configured, "abc.def", "sk-live", "sk-****", base64_run, "0123456789abcdef"):
        assert leaked not in message
        assert leaked not in caught.value.detail
    assert "[redacted]" in caught.value.detail
    assert "response_format is not supported" in caught.value.detail


async def test_error_text_stays_a_bounded_excerpt(scripted) -> None:
    endpoint = scripted(scripted.api_error(400, "word " * 1000))
    async with make_client(endpoint) as client:
        with pytest.raises(EndpointError) as caught:
            await client.post("/x", {})
    assert 0 < len(caught.value.detail) <= 300


async def test_proxy_environment_is_ignored_by_default(scripted, client_options) -> None:
    endpoint = scripted(httpx.Response(200, json={}))
    async with make_client(endpoint) as client:
        await client.post("/x", {})
    assert [options["trust_env"] for options in client_options] == [False]


async def test_trust_env_lets_httpx_read_the_proxy_environment(scripted, client_options) -> None:
    endpoint = scripted(httpx.Response(200, json={}))
    async with EndpointClient(
        base_url="http://llm.test/v1",
        api_key=None,
        timeout_s=5,
        retry=RetrySettings(),
        trust_env=True,
        transport=endpoint.transport,
    ) as client:
        await client.post("/x", {})
    assert [options["trust_env"] for options in client_options] == [True]
