"""Every tool's output, byte for byte, against a snapshot taken before Herald moved to the typed
client: the text and JSON shapes, the resources and the error texts must not drift.

Regenerate with `UPDATE_SNAPSHOTS=1 pytest packages/herald/tests/test_snapshots.py` only when an
output is meant to change.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.types import TextContent

from .conftest import Connect
from .fake_keeper import (
    API,
    CONTEXT_PACK,
    EXECUTION,
    EXPLAINED,
    MARKDOWN_PACK,
    OLDER,
    PARENT,
    REVISION,
    SCHEMA,
    SUPPLIER_RECORD,
    FakeKeeper,
    branch_entry,
    problem,
    record,
    revision_log,
    served,
)

SNAPSHOT = Path(__file__).with_name("snapshots") / "tool_outputs.json"
DOMAIN = {
    "id": "supply",
    "display_name": "Supply",
    "default_branch": "main",
    "created_at": "2026-10-01T08:00:00Z",
    "created_by": "usr_loader",
}
QUERY = {
    "kind": "Part",
    "where": {"attr": "category", "op": "eq", "value": "bearing"},
    "order_by": [{"attr": "unit_cost", "dir": "desc"}],
    "limit": 20,
}
DIFF = {
    "from_revision": PARENT,
    "to_revision": REVISION,
    "total": 3,
    "summary": [
        {
            "partition": "k",
            "element": "record",
            "name": "Supplier",
            "added": 1,
            "modified": 1,
            "removed": 0,
        },
        {
            "partition": "r",
            "element": "link",
            "name": "supplied_by",
            "added": 0,
            "modified": 0,
            "removed": 1,
        },
    ],
    "items": [
        {
            "element": "record",
            "change": "modified",
            "partition": "k",
            "kind": "Supplier",
            "key": "S-1",
            "changes": [{"name": "city", "old": "Poznan", "new": "Lodz"}],
        },
        {
            "element": "record",
            "change": "added",
            "partition": "k",
            "kind": "Supplier",
            "key": "S-2",
        },
        {
            "element": "link",
            "change": "removed",
            "partition": "r",
            "relation": "supplied_by",
            "from_key": "P-1",
            "to_key": "S-1",
        },
    ],
    "next_cursor": "d2",
}
COMMITTED = {**EXECUTION, "status": "committed", "revision": PARENT, "revisions": [PARENT]}
REJECTED = {
    **EXECUTION,
    "edits": [],
    "edit_count": 0,
    "violations": [{"code": "required", "path": ["parameters", "tier"], "message": "is required"}],
    "violation_count": 1,
    "warnings": [{"code": "stale", "path": [], "message": "tier is old"}],
    "review_required": True,
    "replayed": True,
}


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def execute(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["parameters"].get("reject"):
        return httpx.Response(200, json=REJECTED)
    return httpx.Response(200, json=EXECUTION if body["dry_run"] else COMMITTED)


def pack(request: httpx.Request) -> httpx.Response:
    if json.loads(request.content)["format"] == "json":
        return httpx.Response(200, json=CONTEXT_PACK)
    return httpx.Response(200, text=MARKDOWN_PACK, headers={"content-type": "text/markdown"})


def routes(fake: FakeKeeper) -> None:
    items = [
        {
            **record("Part", "P-1", "Bearing", category="bearing", unit_cost="10.79"),
            "links": [{"relation": "supplied_by", "direction": "forward", "count": 1}],
            "included": {"supplied_by": [{"kind": "Supplier", "key": "S-1", "title": "Acme"}]},
        },
        {
            **record("Part", "P-2", "", category="bearing", unit_cost="9.5", note="a\nb"),
            "title": None,
        },
    ]
    edge = {
        "relation": "supplied_by",
        "from": {"kind": "Part", "key": "P-1"},
        "to": {"kind": "Supplier", "key": "S-1"},
        "values": {"since": "2026"},
    }
    for method, path, answer in (
        ("GET", f"{API}/schema", SCHEMA),
        ("GET", f"{API}/kinds/Supplier/records/S-1001", SUPPLIER_RECORD),
        ("GET", f"{API}/kinds/Supplier/records/S-1001/explain", EXPLAINED),
        (
            "POST",
            f"{API}/query",
            served(
                items=items,
                next_cursor="c2",
                aggregate={"counts": [{"kind": "Part", "value": "bearing", "count": 2}]},
            ),
        ),
        (
            "POST",
            f"{API}/search",
            served(
                mode="lexical",
                items=[
                    {
                        "kind": "Part",
                        "key": "P-1",
                        "title": "Bearing",
                        "snippet": "a\nb",
                        "score": 0.5,
                    },
                    {"kind": "Part", "key": "P-2", "score": 0.123456789},
                ],
            ),
        ),
        (
            "POST",
            f"{API}/neighborhood",
            served(
                nodes=[
                    record("Supplier", "S-1", "Acme", tier="1"),
                    {**record("Part", "P-1", "Bearing"), "links": []},
                ],
                edges=[edge],
                truncated=True,
            ),
        ),
        ("POST", f"{API}/context", pack),
        ("GET", "/api/v1/domains/supply", DOMAIN),
        ("GET", f"{API}/branches/main/log", revision_log),
        ("GET", f"{API}/diff", DIFF),
        (
            "GET",
            f"{API}/branches",
            {
                "items": [
                    branch_entry("main", REVISION, default=True, protected=True),
                    branch_entry("agent/x", PARENT, default=False, protected=False),
                ],
                "next_cursor": "b2",
            },
        ),
        ("POST", f"{API}/operations/update_supplier/execute", execute),
    ):
        fake.on(method, path, answer)


Arguments = dict[str, Any]
CASES: dict[str, tuple[str, Arguments]] = {
    "schema-text": ("lorebase_schema", {}),
    "schema-full-text": ("lorebase_schema", {"detail": "full", "branch": "dev"}),
    "schema-json": ("lorebase_schema", {"kinds": ["Supplier"], "format": "json"}),
    "get-text": ("lorebase_get", {"ref": "Supplier/S-1001"}),
    "get-json": ("lorebase_get", {"ref": "Supplier/S-1001", "format": "json"}),
    "get-at-text": ("lorebase_get", {"ref": "Supplier/S-1001", "at": "v1", "links": False}),
    "query-text": ("lorebase_query", {"query": QUERY, "cursor": "c1"}),
    "query-json": ("lorebase_query", {"query": QUERY, "format": "json"}),
    "search-text": ("lorebase_search", {"query": "bearing", "kinds": ["Part"], "limit": 5}),
    "search-json": ("lorebase_search", {"query": "bearing", "format": "json"}),
    "search-vector-text": ("lorebase_search", {"query_vector": [0.1, 0.2]}),
    "neighborhood-text": ("lorebase_neighborhood", {"seeds": ["Part/P-1"], "depth": 2}),
    "neighborhood-values-json": (
        "lorebase_neighborhood",
        {"seeds": ["Part/P-1"], "values": True, "max_edges": 10, "format": "json"},
    ),
    "context-markdown": ("lorebase_context", {"records": ["Supplier/S-1001"], "depth": 2}),
    "context-json": ("lorebase_context", {"records": ["Supplier/S-1001"], "format": "json"}),
    "context-question-markdown": ("lorebase_context", {"question": "kavsen"}),
    "context-question-json": ("lorebase_context", {"question": "kavsen", "format": "json"}),
    "explain-text": ("lorebase_explain", {"ref": "Supplier/S-1001"}),
    "explain-attributes-json": (
        "lorebase_explain",
        {"ref": "Supplier/S-1001", "attributes": ["tier"], "format": "json"},
    ),
    "changes-text": ("lorebase_changes", {"since": OLDER[:10]}),
    "changes-latest-json": ("lorebase_changes", {"limit": 1, "format": "json"}),
    "changes-unreached-text": ("lorebase_changes", {"since": "ff" * 32, "limit": 2}),
    "diff-text": ("lorebase_diff", {"from_revision": "v1", "to_revision": "main"}),
    "diff-json": (
        "lorebase_diff",
        {"from_revision": "v1", "to_revision": "main", "kinds": ["Supplier"], "format": "json"},
    ),
    "execute-dry-run-text": ("lorebase_execute", {"operation": "update_supplier"}),
    "execute-committed-json": (
        "lorebase_execute",
        {
            "operation": "update_supplier",
            "dry_run": False,
            "expected_head": REVISION,
            "format": "json",
        },
    ),
    "execute-rejected-text": (
        "lorebase_execute",
        {"operation": "update_supplier", "parameters": {"reject": True}},
    ),
    "branches-text": ("lorebase_branches", {}),
    "branches-json": ("lorebase_branches", {"format": "json"}),
    "bad-ref": ("lorebase_get", {"ref": "x"}),
    "query-required": ("lorebase_search", {}),
    "seeds-required": ("lorebase_context", {}),
}
Problem = Callable[[], httpx.Response | Callable[[httpx.Request], httpx.Response]]
FAILURES: dict[str, tuple[str, str, Problem, str, Arguments]] = {
    "stale-head": (
        "POST",
        f"{API}/operations/update_supplier/execute",
        lambda: problem(
            409, "stale-head", "Branch 'main' moved.", current_head=REVISION, expected_head="00"
        ),
        "lorebase_execute",
        {"operation": "update_supplier", "dry_run": False},
    ),
    "stale-head-unknown": (
        "POST",
        f"{API}/operations/update_supplier/execute",
        lambda: problem(409, "stale-head", "Branch 'main' moved."),
        "lorebase_execute",
        {"operation": "update_supplier", "dry_run": False},
    ),
    "review-required": (
        "POST",
        f"{API}/operations/update_supplier/execute",
        lambda: problem(409, "review-required", "Edits need review.", proposals="/proposals"),
        "lorebase_execute",
        {"operation": "update_supplier", "dry_run": False},
    ),
    "protected-branch": (
        "POST",
        f"{API}/operations/update_supplier/execute",
        lambda: problem(409, "protected-branch", "Branch 'main' is protected."),
        "lorebase_execute",
        {"operation": "update_supplier", "dry_run": False},
    ),
    "idempotency-conflict": (
        "POST",
        f"{API}/operations/update_supplier/execute",
        lambda: problem(409, "idempotency-conflict", "Key used with other parameters."),
        "lorebase_execute",
        {"operation": "update_supplier", "idempotency_key": "k-1"},
    ),
    "validation-issues": (
        "POST",
        f"{API}/operations/update_supplier/execute",
        lambda: problem(
            422,
            "validation-failed",
            "3 validation issue(s); nothing was written.",
            issues=[
                {
                    "code": "required",
                    "path": ["parameters", f"p{i}"],
                    "message": "is required",
                    "kind": "Supplier",
                    "key": "S-1",
                }
                for i in range(3)
            ],
            issue_count=5,
        ),
        "lorebase_execute",
        {"operation": "update_supplier"},
    ),
    "request-errors": (
        "POST",
        f"{API}/query",
        lambda: problem(
            422,
            "validation-failed",
            "Invalid query.",
            errors=[
                {"pointer": "/body/where/op", "message": "unknown operator", "code": "invalid"}
            ],
        ),
        "lorebase_query",
        {"query": {"kind": "Part"}},
    ),
    "not-found": (
        "GET",
        f"{API}/kinds/Supplier/records/S-1001",
        lambda: problem(404, "not-found", "Record Supplier/S-1001 not found."),
        "lorebase_get",
        {"ref": "Supplier/S-1001"},
    ),
    "forbidden": (
        "GET",
        f"{API}/schema",
        lambda: problem(403, "forbidden", "No schema.read."),
        "lorebase_schema",
        {},
    ),
    "unauthorized": (
        "GET",
        f"{API}/schema",
        lambda: problem(401, "unauthorized", "Authentication required."),
        "lorebase_schema",
        {},
    ),
    "rate-limited": (
        "GET",
        f"{API}/schema",
        lambda: httpx.Response(
            429,
            json={"status": 429, "code": "rate-limited", "title": "Too many requests"},
            headers={"retry-after": "7", "content-type": "application/problem+json"},
        ),
        "lorebase_schema",
        {},
    ),
    "branch-busy": (
        "POST",
        f"{API}/operations/update_supplier/execute",
        lambda: problem(503, "branch-busy", "Another writer holds the branch."),
        "lorebase_execute",
        {"operation": "update_supplier", "dry_run": False},
    ),
    "branch-not-materialized": (
        "GET",
        f"{API}/kinds/Supplier/records/S-1001",
        lambda: problem(409, "branch-not-materialized", "Read it at a Revision."),
        "lorebase_get",
        {"ref": "Supplier/S-1001"},
    ),
    "limit-exceeded": (
        "POST",
        f"{API}/neighborhood",
        lambda: problem(422, "limit-exceeded", "Too many nodes."),
        "lorebase_neighborhood",
        {"seeds": ["Part/P-1"]},
    ),
    "internal-error": (
        "GET",
        f"{API}/diff",
        lambda: problem(500, "internal-error", "Internal server error"),
        "lorebase_diff",
        {"from_revision": "a", "to_revision": "b"},
    ),
    "non-problem": (
        "GET",
        f"{API}/schema",
        lambda: httpx.Response(502, text="bad gateway"),
        "lorebase_schema",
        {},
    ),
    "unreachable": ("GET", f"{API}/schema", lambda: unreachable, "lorebase_schema", {}),
}


async def run(connect: Connect, name: str, arguments: Arguments, **settings: Any) -> dict[str, Any]:
    async with connect(**settings) as client:
        result = await client.call_tool(name, arguments)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return {"error": bool(result.is_error), "text": block.text}


async def observe(connect: Connect, fake: FakeKeeper, name: str) -> dict[str, Any]:
    routes(fake)
    if name in CASES:
        tool, arguments = CASES[name]
        return await run(connect, tool, arguments)
    method, path, answer, tool, arguments = FAILURES[name]
    fake.on(method, path, answer())
    return await run(connect, tool, arguments, output={"max_issues": 2})


def stored() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}


@pytest.mark.parametrize("name", [*CASES, *FAILURES])
async def test_output_matches_snapshot(connect: Connect, fake: FakeKeeper, name: str) -> None:
    observed = await observe(connect, fake, name)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps({**stored(), name: observed}, indent=2, sort_keys=True) + "\n"
        )
        return
    assert observed == stored()[name]


async def test_resources_match_snapshot(connect: Connect, fake: FakeKeeper) -> None:
    routes(fake)
    async with connect() as client:
        schema = await client.read_resource("lorebase://supply/schema")
        found = await client.read_resource("lorebase://supply/Supplier/S-1001")
    observed = {
        "error": False,
        "text": schema.contents[0].text + "\n--\n" + found.contents[0].text,  # type: ignore[union-attr]
    }
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps({**stored(), "resources": observed}, indent=2, sort_keys=True) + "\n"
        )
        return
    assert observed == stored()["resources"]
