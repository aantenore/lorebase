"""Credentials, error mapping and configuration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.types import TextContent

from lorebase_client import LorebaseConnectionError, RateLimitedError, ServerError
from lorebase_herald import __main__ as herald_main
from lorebase_herald.errors import tool_error
from lorebase_herald.keeper import KeeperClients
from lorebase_herald.server import Credentials
from lorebase_herald.settings import herald_settings
from lorebase_platform.settings import CONFIG_ENV_VAR, load_settings

from .conftest import AGENT_KEY, Connect
from .conftest import herald_settings as make_settings
from .fake_keeper import API, REVISION, SCHEMA, FakeKeeper, problem

EXECUTE = f"{API}/operations/flag_supplier_risk/execute"


async def _error(connect: Connect, tool: str, arguments: dict[str, Any], **settings: Any) -> str:
    async with connect(**settings) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# Errors --------------------------------------------------------------------------------------


async def test_stale_head_names_the_current_head(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "POST",
        EXECUTE,
        problem(
            409,
            "stale-head",
            "Branch 'main' moved.",
            branch="main",
            expected_head="00",
            current_head=REVISION,
        ),
    )
    message = await _error(
        connect, "lorebase_execute", {"operation": "flag_supplier_risk", "dry_run": False}
    )
    assert "[stale-head] Branch 'main' moved." in message
    assert f"next: the branch head is now {REVISION}" in message
    assert f"expected_head={REVISION}" in message


async def test_review_required_points_to_proposals(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "POST",
        EXECUTE,
        problem(
            409,
            "review-required",
            "Edits need review.",
            proposals="/api/v1/domains/supply/proposals",
        ),
    )
    message = await _error(
        connect, "lorebase_execute", {"operation": "flag_supplier_risk", "dry_run": False}
    )
    assert "[review-required]" in message
    assert "open a Proposal" in message
    assert "/api/v1/domains/supply/proposals" in message
    assert "do not retry here" in message


async def test_validation_issues_are_listed_and_capped(connect: Connect, fake: FakeKeeper) -> None:
    issues = [
        {
            "code": "required",
            "severity": "error",
            "path": ["parameters", f"p{i}"],
            "message": "is required",
            "kind": "Supplier",
        }
        for i in range(5)
    ]
    fake.on(
        "POST",
        EXECUTE,
        problem(
            422,
            "validation-failed",
            "5 validation issue(s); nothing was written.",
            issues=issues,
            issue_count=7,
        ),
    )
    message = await _error(
        connect,
        "lorebase_execute",
        {"operation": "flag_supplier_risk"},
        output={"max_issues": 2},
    )
    lines = message.splitlines()
    assert "- parameters/p0 (Supplier): is required [required]" in lines
    assert "- parameters/p2 (Supplier): is required [required]" not in lines
    assert "- ... 5 more" in lines
    assert lines[-1] == "next: fix the listed members and call again"


async def test_request_validation_errors_use_pointers(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "POST",
        f"{API}/query",
        problem(
            422,
            "validation-failed",
            "Invalid query.",
            errors=[
                {"pointer": "/body/where/op", "message": "unknown operator", "code": "invalid"}
            ],
        ),
    )
    message = await _error(connect, "lorebase_query", {"query": {"kind": "Part"}})
    assert "- /body/where/op: unknown operator [invalid]" in message


def test_rate_limits_and_server_errors_suggest_retrying() -> None:
    limited = tool_error(
        RateLimitedError("Too many requests", status=429, code="rate-limited", retry_after=7.0),
        max_issues=5,
    )
    assert str(limited).endswith("next: retry after 7 s")
    broken = tool_error(
        ServerError("Internal server error", status=500, code="internal-error"), max_issues=5
    )
    assert str(broken).endswith("next: retry later")
    down = tool_error(LorebaseConnectionError("cannot reach the server"), max_issues=5)
    assert str(down).startswith("[keeper-unavailable]")


async def test_throttled_calls_are_retried_before_they_fail(
    connect: Connect, fake: FakeKeeper
) -> None:
    answers = iter([problem(429, "rate-limited", None), httpx.Response(200, json=SCHEMA)])
    fake.on("GET", f"{API}/schema", lambda _: next(answers))
    keeper = {"base_url": "http://keeper.test", "api_key": AGENT_KEY}
    retrying = {"max_retries": 1, "retry_max_wait_s": 0.01}
    async with connect(keeper={**keeper, **retrying}) as client:
        result = await client.call_tool("lorebase_schema", {})
    assert not result.is_error
    assert len(fake.requests) == 2
    fake.requests.clear()
    fake.on("GET", f"{API}/schema", problem(429, "rate-limited", None))
    message = await _error(connect, "lorebase_schema", {}, keeper={**keeper, **retrying})
    assert message.endswith("next: retry later")
    assert len(fake.requests) == 2


async def test_a_query_the_client_models_refuse_is_not_sent(
    connect: Connect, fake: FakeKeeper
) -> None:
    message = await _error(
        connect, "lorebase_query", {"query": {"kind": "Part", "group_by": "category"}}
    )
    assert "[validation-failed] the request is not valid" in message
    assert "- group_by: Extra inputs are not permitted [extra_forbidden]" in message
    assert message.endswith("next: fix the listed members and call again")
    assert fake.requests == []


async def test_an_answer_that_does_not_fit_the_models_is_a_tool_error(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on("GET", f"{API}/schema", {"domain": "supply"})
    message = await _error(connect, "lorebase_schema", {})
    assert "[keeper-response-invalid]" in message
    assert message.endswith("next: use matching Herald and Keeper versions")


async def test_unreachable_keeper_is_a_tool_error(connect: Connect, fake: FakeKeeper) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake.on("GET", f"{API}/schema", refuse)
    message = await _error(connect, "lorebase_schema", {})
    assert message.endswith(
        "[keeper-unavailable] Keeper is unreachable (ConnectError). next: retry later"
    )


async def test_a_keeper_that_does_not_answer_in_time_is_a_tool_error(
    connect: Connect, fake: FakeKeeper
) -> None:
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    fake.on("GET", f"{API}/schema", stall)
    message = await _error(connect, "lorebase_schema", {})
    assert message.endswith("[keeper-unavailable] Keeper did not answer in time. next: retry later")


async def test_non_problem_errors_keep_the_status(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", httpx.Response(502, text="bad gateway"))
    message = await _error(connect, "lorebase_schema", {})
    assert "[http-502]" in message


async def test_domain_is_required_when_none_is_configured(connect: Connect) -> None:
    message = await _error(connect, "lorebase_schema", {}, domain=None)
    assert "[domain-required]" in message
    assert "LOREBASE_DOMAIN" in message


# Credentials ---------------------------------------------------------------------------------


class _Ctx:
    def __init__(self, headers: Mapping[str, str] | None) -> None:
        self.headers = headers


def test_stdio_uses_the_configured_agent_key() -> None:
    assert Credentials(make_settings())(None, "t") == AGENT_KEY
    assert Credentials(make_settings(keeper={"api_key": None}))(None, "t") is None


def test_http_forwards_the_callers_credential_and_never_the_configured_key() -> None:
    credentials = Credentials(make_settings(transport="streamable_http"))
    forwarded = credentials(_Ctx({"authorization": "Bearer lbk_caller_secret"}), "t")  # type: ignore[arg-type]
    assert forwarded == "Bearer lbk_caller_secret"
    with pytest.raises(Exception, match=r"\[unauthorized\]"):
        credentials(_Ctx({}), "t")  # type: ignore[arg-type]
    with pytest.raises(Exception, match=r"\[unauthorized\]"):
        credentials(None, "t")


def test_http_refuses_calls_without_credential_by_default() -> None:
    configured = make_settings(transport="streamable_http")
    assert configured.http.allow_anonymous is False
    with pytest.raises(Exception, match=r"\[unauthorized\]"):
        Credentials(configured)(_Ctx({}), "t")  # type: ignore[arg-type]


def test_http_anonymous_forwards_no_credential_and_never_the_configured_key() -> None:
    credentials = Credentials(
        make_settings(transport="streamable_http", http={"allow_anonymous": True})
    )
    assert credentials(_Ctx({}), "t") is None  # type: ignore[arg-type]
    assert credentials(None, "t") is None
    forwarded = credentials(_Ctx({"authorization": "Bearer lbk_caller_secret"}), "t")  # type: ignore[arg-type]
    assert forwarded == "Bearer lbk_caller_secret"


async def test_http_anonymous_calls_reach_keeper_without_authorization(
    connect: Connect, fake: FakeKeeper
) -> None:
    # Keeper in auth mode none answers them.
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect(transport="streamable_http", http={"allow_anonymous": True}) as client:
        result = await client.call_tool("lorebase_schema", {})
    assert not result.is_error
    assert "authorization" not in fake.last().headers


async def test_http_anonymous_calls_fail_when_keeper_requires_authentication(
    connect: Connect, fake: FakeKeeper
) -> None:
    # Keeper in any other mode refuses them; Herald does not let them through.
    fake.on("GET", f"{API}/schema", problem(401, "unauthorized", "Authentication required."))
    message = await _error(
        connect, "lorebase_schema", {}, transport="streamable_http", http={"allow_anonymous": True}
    )
    assert "authorization" not in fake.last().headers
    assert "[unauthorized]" in message


async def test_every_call_carries_the_agent_key(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", problem(403, "forbidden", "No schema.read."))
    message = await _error(connect, "lorebase_schema", {})
    assert fake.last().headers["authorization"] == f"Bearer {AGENT_KEY}"
    assert "[forbidden]" in message
    assert "do not retry" in message


async def test_each_request_opens_a_client_with_its_own_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOREBASE_API_KEY", "lbk_ambient_key")
    fake = FakeKeeper()
    fake.on("GET", "/api/v1/domains", {"items": []})
    clients = KeeperClients(make_settings().keeper, transport=fake.transport())
    async with clients.open("Bearer abc") as first:
        await first.domains.list()
        assert fake.last().headers["authorization"] == "Bearer abc"
    async with clients.open("lbk_bare_key") as second:
        await second.domains.list()
        assert fake.last().headers["authorization"] == "Bearer lbk_bare_key"
    async with clients.open(None) as third:
        await third.domains.list()
        assert "authorization" not in fake.last().headers  # nor the ambient key
    assert first is not second is not third
    assert fake.last().headers["user-agent"] == "lorebase-herald"


@pytest.mark.parametrize(("configured", "expected"), [({}, False), ({"trust_env": True}, True)])
async def test_keeper_clients_ignore_proxy_variables_unless_trust_env(
    monkeypatch: pytest.MonkeyPatch, configured: dict[str, Any], expected: bool
) -> None:
    built: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def recording(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        built.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", recording)
    keeper = make_settings().keeper.model_copy(update=configured)
    async with KeeperClients(keeper).open(None):
        pass
    assert [options["trust_env"] for options in built] == [expected]


# Configuration -------------------------------------------------------------------------------


def test_short_environment_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE_URL", "https://lorebase.example.com/")
    monkeypatch.setenv("LOREBASE_API_KEY", "lbk_env_key")
    monkeypatch.setenv("LOREBASE_DOMAIN", "supply")
    monkeypatch.setenv("LOREBASE__HERALD__TRANSPORT", "streamable_http")
    monkeypatch.setenv("LOREBASE__HERALD__OUTPUT__MAX_CHARS", "5000")
    configured = herald_settings(load_settings())
    assert configured.keeper.base_url == "https://lorebase.example.com"
    assert configured.keeper.api_key is not None
    assert configured.keeper.api_key.get_secret_value() == "lbk_env_key"
    assert configured.domain == "supply"
    assert configured.transport == "streamable_http"
    assert configured.output.max_chars == 5000


def test_api_key_accepts_secret_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_KEY_FOR_TEST", "lbk_from_reference")
    monkeypatch.setenv("LOREBASE_API_KEY", "secret://env/AGENT_KEY_FOR_TEST")
    configured = herald_settings(load_settings())
    assert configured.keeper.api_key is not None
    assert configured.keeper.api_key.get_secret_value() == "lbk_from_reference"


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = herald_settings(load_settings())
    example = Path(__file__).resolve().parents[3] / "config" / "lorebase.example.yaml"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(example))
    assert herald_settings(load_settings()) == defaults


def test_request_urls_with_record_keys_stay_out_of_the_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx logs each request URL (Record keys included) at INFO; Herald raises it to WARNING."""
    started: list[Any] = []

    async def serve(configured: Any, clients: Any) -> None:
        started.append(configured)

    monkeypatch.setattr(herald_main, "serve", serve)
    monkeypatch.setattr(herald_main, "configure_logging", lambda *a, **k: None)
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)

    herald_main.main()

    assert started
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
