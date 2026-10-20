"""Credentials, error mapping and configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.types import TextContent

from lorebase_herald.errors import tool_error
from lorebase_herald.keeper_http import KeeperError, KeeperHttp, KeeperUnavailableError
from lorebase_herald.server import Credentials
from lorebase_herald.settings import herald_settings
from lorebase_platform.settings import CONFIG_ENV_VAR, load_settings

from .conftest import AGENT_KEY, Connect
from .conftest import herald_settings as make_settings
from .fake_keeper import API, REVISION, FakeKeeper, problem

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
        KeeperError(429, "rate-limited", "Too many requests", retry_after="7"), max_issues=5
    )
    assert str(limited).endswith("next: retry after 7 s")
    broken = tool_error(KeeperError(500, "internal-error", "Internal server error"), max_issues=5)
    assert str(broken).endswith("next: retry later")
    down = tool_error(KeeperUnavailableError("Keeper is unreachable"), max_issues=5)
    assert str(down).startswith("[keeper-unavailable]")


async def test_unreachable_keeper_is_a_tool_error(connect: Connect, fake: FakeKeeper) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake.on("GET", f"{API}/schema", refuse)
    message = await _error(connect, "lorebase_schema", {})
    assert message.endswith(
        "[keeper-unavailable] Keeper is unreachable (ConnectError). next: retry later"
    )


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
    assert Credentials(make_settings())(None) == AGENT_KEY
    assert Credentials(make_settings(keeper={"api_key": None}))(None) is None


def test_http_forwards_the_callers_credential_and_never_the_configured_key() -> None:
    credentials = Credentials(make_settings(transport="streamable_http"))
    forwarded = credentials(_Ctx({"authorization": "Bearer lbk_caller_secret"}))  # type: ignore[arg-type]
    assert forwarded == "Bearer lbk_caller_secret"
    with pytest.raises(Exception, match=r"\[unauthorized\]"):
        credentials(_Ctx({}))  # type: ignore[arg-type]
    with pytest.raises(Exception, match=r"\[unauthorized\]"):
        credentials(None)


async def test_every_call_carries_the_agent_key(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", problem(403, "forbidden", "No schema.read."))
    message = await _error(connect, "lorebase_schema", {})
    assert fake.last().headers["authorization"] == f"Bearer {AGENT_KEY}"
    assert "[forbidden]" in message
    assert "do not retry" in message


async def test_keeper_http_forwards_bearer_values_as_they_are() -> None:
    fake = FakeKeeper()
    fake.on("GET", "/api/v1/domains", {"items": []})
    keeper = KeeperHttp(make_settings().keeper, transport=fake.transport())
    try:
        await keeper.get("/domains", credential="Bearer abc")
        assert fake.last().headers["authorization"] == "Bearer abc"
        await keeper.get("/domains", credential=None)
        assert "authorization" not in fake.last().headers
    finally:
        await keeper.aclose()


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
