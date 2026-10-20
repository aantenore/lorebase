"""Error mapping from problem details and configuration resolution."""

from __future__ import annotations

import httpx
import pytest

from lorebase_client import (
    AlreadyExistsError,
    ApiError,
    AuthenticationError,
    BranchBusyError,
    ConflictError,
    IdempotencyConflictError,
    LorebaseConfigError,
    LorebaseError,
    NotFoundError,
    PermissionDeniedError,
    ProtectedBranchError,
    RateLimitedError,
    ReviewRequiredError,
    ServerError,
    StaleHeadError,
    UnavailableError,
    ValidationFailedError,
    error_from_response,
    resolve_config,
)
from lorebase_client.errors import parse_retry_after

PROBLEM = "application/problem+json"


def problem(status: int, code: str, **members: object) -> httpx.Response:
    body = {
        "type": f"https://lorebase.dev/problems/{code}",
        "title": "Title",
        "status": status,
        "code": code,
        "detail": f"detail of {code}",
        "instance": "/api/v1/x",
        "request_id": "req-1",
        **members,
    }
    return httpx.Response(status, json=body, headers={"content-type": PROBLEM})


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "unauthorized", AuthenticationError),
        (403, "forbidden", PermissionDeniedError),
        (404, "not-found", NotFoundError),
        (409, "conflict", ConflictError),
        (409, "stale-head", StaleHeadError),
        (409, "protected-branch", ProtectedBranchError),
        (409, "review-required", ReviewRequiredError),
        (409, "already-exists", AlreadyExistsError),
        (409, "idempotency-conflict", IdempotencyConflictError),
        (422, "validation-failed", ValidationFailedError),
        (422, "schema-change-invalid", ValidationFailedError),
        (429, "rate-limited", RateLimitedError),
        (500, "internal-error", ServerError),
        (503, "unavailable", UnavailableError),
        (503, "branch-busy", BranchBusyError),
    ],
)
def test_problem_code_and_status_choose_the_exception(
    status: int, code: str, expected: type[ApiError]
) -> None:
    error = error_from_response(problem(status, code))
    assert type(error) is expected
    assert isinstance(error, LorebaseError)
    assert error.status == status
    assert error.code == code
    assert error.type == f"https://lorebase.dev/problems/{code}"
    assert error.detail == f"detail of {code}"
    assert error.request_id == "req-1"
    assert str(error) == f"detail of {code} (request req-1)"


def test_extra_members_are_kept_and_exposed() -> None:
    stale = error_from_response(
        problem(409, "stale-head", branch="work", expected_head="aa", current_head="bb")
    )
    assert stale.current_head == "bb"
    assert stale.extra == {"branch": "work", "expected_head": "aa", "current_head": "bb"}
    invalid = error_from_response(
        problem(
            422,
            "validation-failed",
            issues=[{"code": "required", "path": ["parameters", "rows", 0], "message": "m"}],
            issue_count=1,
            errors=[{"pointer": "/body/x", "message": "bad", "code": "invalid"}],
        )
    )
    assert invalid.issues[0]["code"] == "required"
    assert invalid.errors[0]["pointer"] == "/body/x"
    assert error_from_response(problem(403, "forbidden", permission="data.read")).extra == {
        "permission": "data.read"
    }
    assert error_from_response(problem(404, "not-found")).issues == []


def test_retry_after_header_is_read_in_seconds() -> None:
    response = problem(503, "branch-busy")
    response.headers["Retry-After"] = "7"
    assert error_from_response(response).retry_after == 7.0
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:10 GMT", now=1792567680.0) == 10.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None


def test_non_problem_responses_still_map_by_status() -> None:
    gateway = httpx.Response(502, text="<html>Bad gateway</html>")
    error = error_from_response(gateway)
    assert type(error) is ServerError
    assert error.code is None
    assert "502" in str(error)
    assert "Bad gateway" in str(error)
    assert type(error_from_response(httpx.Response(418, text="teapot"))) is ApiError
    assert type(error_from_response(httpx.Response(503, text=""))) is UnavailableError


def test_configuration_precedence_arguments_then_environment() -> None:
    env = {
        "LOREBASE_URL": "http://env.test",
        "LOREBASE_API_KEY": "lbk_env",
        "LOREBASE_TOKEN": "tok_env",
        "LOREBASE_TIMEOUT": "12.5",
        "LOREBASE_API_PREFIX": "/v2",
    }
    from_env = resolve_config(env=env)
    assert from_env.base_url == "http://env.test"
    assert from_env.root == "http://env.test/v2"
    assert from_env.timeout_s == 12.5
    assert from_env.bearer() == "lbk_env"  # the API key wins over the token
    explicit = resolve_config(
        "http://arg.test/", api_key="lbk_arg", timeout=3, api_prefix="/api/v1", env=env
    )
    assert explicit.root == "http://arg.test/api/v1"
    assert explicit.bearer() == "lbk_arg"
    assert explicit.timeout_s == 3
    token_only = resolve_config("http://x.test", env={"LOREBASE_TOKEN": "tok_env"})
    assert token_only.bearer() == "tok_env"


def test_token_may_be_a_callable_and_anonymous_is_allowed() -> None:
    tokens = iter(["one", "two"])
    config = resolve_config("http://x.test", token=lambda: next(tokens), env={})
    assert [config.bearer(), config.bearer()] == ["one", "two"]
    assert resolve_config("http://x.test", env={}).bearer() is None


def test_missing_url_and_bad_timeout_are_configuration_errors() -> None:
    with pytest.raises(LorebaseConfigError, match="LOREBASE_URL"):
        resolve_config(env={})
    with pytest.raises(LorebaseConfigError, match="LOREBASE_TIMEOUT"):
        resolve_config("http://x.test", env={"LOREBASE_TIMEOUT": "soon"})


def test_cache_option_sets_the_number_of_entries() -> None:
    assert resolve_config("http://x.test", env={}).cache_entries is None
    assert resolve_config("http://x.test", cache=True, env={}).cache_entries == 256
    assert resolve_config("http://x.test", cache=5, env={}).cache_entries == 5
