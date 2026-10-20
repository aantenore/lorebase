"""Domain and branch routes without a database, and branch name rules."""

from __future__ import annotations

import httpx
import pytest

from lorebase_keeper.errors import ProblemError
from lorebase_keeper.services.branches import check_ref_name
from lorebase_keeper.services.config import BranchSettings


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/domains"),
        ("POST", "/api/v1/domains"),
        ("GET", "/api/v1/domains/supply/branches/agent/bot/task"),
        ("DELETE", "/api/v1/domains/supply/branches/main"),
    ],
)
async def test_write_routes_need_a_database(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    body = {"id": "supply"} if method == "POST" else None
    response = await client.request(method, path, json=body)
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("name", ["main", "agent/bot-1/task.2", "source/erp.vendors"])
def test_valid_branch_names(name: str) -> None:
    check_ref_name(name, BranchSettings())


@pytest.mark.parametrize("name", ["", "a b", "/lead", "trail/", "a//b", "-x", "a" * 201])
def test_invalid_branch_names(name: str) -> None:
    with pytest.raises(ProblemError) as raised:
        check_ref_name(name, BranchSettings())
    assert raised.value.status_code == 422
