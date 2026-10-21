"""Each tool's input schema and output, against a fake Keeper over `httpx.MockTransport`."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from .conftest import AGENT_KEY, Connect
from .fake_keeper import (
    API,
    OLDER,
    PARENT,
    REVISION,
    SCHEMA,
    SUPPLIER_RECORD,
    FakeKeeper,
    problem,
    record,
)

TOOLS = {
    "lorebase_schema",
    "lorebase_get",
    "lorebase_query",
    "lorebase_search",
    "lorebase_neighborhood",
    "lorebase_context",
    "lorebase_explain",
    "lorebase_changes",
    "lorebase_diff",
    "lorebase_execute",
    "lorebase_branches",
}


def text(result: CallToolResult) -> str:
    assert not result.is_error, result.content
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def schemas(client: Client) -> dict[str, dict[str, Any]]:
    listed = await client.list_tools()
    return {tool.name: tool.input_schema for tool in listed.tools}


# Schemas -------------------------------------------------------------------------------------


async def test_every_tool_is_listed_with_a_description_and_strict_inputs(
    connect: Connect,
) -> None:
    async with connect() as client:
        listed = await client.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.description
        assert len(tool.description) < 700
        assert tool.output_schema is None  # compact text, not structured output
        assert "ctx" not in tool.input_schema["properties"]
    assert tools["lorebase_execute"].annotations.read_only_hint is False
    assert all(tools[name].annotations.read_only_hint for name in TOOLS - {"lorebase_execute"})


async def test_schemas_carry_required_members_and_bounds_from_settings(connect: Connect) -> None:
    async with connect(neighborhood={"max_depth": 2, "max_seeds": 7}) as client:
        found = await schemas(client)
    assert found["lorebase_get"]["required"] == ["ref"]
    assert found["lorebase_query"]["required"] == ["query"]
    assert found["lorebase_query"]["properties"]["query"]["type"] == "object"
    assert set(found["lorebase_diff"]["required"]) == {"from_revision", "to_revision"}
    assert found["lorebase_execute"]["required"] == ["operation"]
    assert found["lorebase_execute"]["properties"]["dry_run"]["default"] is True
    hood = found["lorebase_neighborhood"]["properties"]
    assert hood["depth"]["maximum"] == 2
    assert hood["seeds"]["maxItems"] == 7
    assert found["lorebase_search"]["properties"]["query_vector"]["anyOf"][0]["type"] == "array"
    assert found["lorebase_schema"]["properties"]["format"]["anyOf"][0]["enum"] == [
        "text",
        "json",
    ]


async def test_execute_description_states_governed_writes(connect: Connect) -> None:
    async with connect() as client:
        listed = await client.list_tools()
    description = next(t.description for t in listed.tools if t.name == "lorebase_execute")
    assert description is not None
    assert "Operation" in description
    assert "Proposal" in description
    assert "dry_run defaults to true" in description
    assert "task" in description
    assert "auto_merge" in description


# Reads ---------------------------------------------------------------------------------------


async def test_schema_summarizes_kinds_relations_and_operations(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect() as client:
        out = text(await client.call_tool("lorebase_schema", {}))
    assert out.splitlines()[0] == f"schema supply@main rev {REVISION}"
    assert "Supplier [key supplier_code; title legal_name]" in out
    assert "supplier_code: string! · legal_name: string! · tier: enum(1|2|3)" in out
    assert "total: decimal(12,2)" in out
    assert "supplier: ref(Supplier)" in out
    assert "ops: create_supplier, update_supplier, delete_supplier" in out
    assert "ops: create_purchase_order" in out
    assert "issued_to: PurchaseOrder -> Supplier (many_to_one; inverse purchase_orders)" in out
    assert "flag_supplier_risk(supplier: ref(Supplier)!) [review: expression]" in out
    assert "The supplier_code." not in out  # summary leaves attribute descriptions out
    request = fake.last()
    assert request.url.params["format"] == "json"
    assert request.headers["authorization"] == f"Bearer {AGENT_KEY}"


async def test_schema_full_json_filtered_to_kinds(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_schema",
                {"kinds": ["Supplier"], "detail": "full", "format": "json", "branch": "dev"},
            )
        )
    view = json.loads(out)
    assert [k["name"] for k in view["kinds"]] == ["Supplier"]
    assert view["kinds"][0]["attributes"][0] == {
        "name": "supplier_code",
        "type": "string",
        "required": True,
        "description": "The supplier_code.",
    }
    assert [r["name"] for r in view["relations"]] == ["issued_to"]
    assert view["revision"] == REVISION
    assert fake.last().url.params["branch"] == "dev"


async def test_get_renders_values_and_link_counts(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/kinds/Supplier/records/S-1001", SUPPLIER_RECORD)
    async with connect() as client:
        out = text(await client.call_tool("lorebase_get", {"ref": "Supplier/S-1001"}))
    assert out.splitlines() == [
        f'Supplier/S-1001 "Kavsen Alloys" rev {REVISION}',
        "supplier_code: S-1001",
        "tier: 1",
        "links: purchase_orders=2",
    ]
    assert fake.last().url.params["links"] == "true"


async def test_get_encodes_keys_and_rejects_bad_refs(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "GET",
        f"{API}/kinds/Doc/records/a b",
        {"revision": REVISION, "record": record("Doc", "a b", "A")},
    )
    async with connect() as client:
        assert "Doc/a b" in text(await client.call_tool("lorebase_get", {"ref": "Doc/a b"}))
        bad = error_text(await client.call_tool("lorebase_get", {"ref": "Supplier"}))
    assert "[invalid-ref]" in bad
    assert "next:" in bad
    assert "%20" in str(fake.requests[0].url)


async def test_query_sends_the_dsl_and_renders_rows(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "POST",
        f"{API}/query",
        {
            "revision": REVISION,
            "items": [
                {
                    **record("Part", "P-1", "Bearing", category="bearing", unit_cost="10.79"),
                    "included": {
                        "supplied_by": [{"kind": "Supplier", "key": "S-1", "title": "Acme"}]
                    },
                }
            ],
            "next_cursor": "c2",
        },
    )
    query = {"kind": "Part", "where": {"attr": "category", "op": "eq", "value": "bearing"}}
    async with connect(branch="dev") as client:
        out = text(await client.call_tool("lorebase_query", {"query": query, "cursor": "c1"}))
    assert fake.last_body() == {**query, "cursor": "c1", "branch": "dev"}
    assert out.splitlines() == [
        f"rev {REVISION}: 1 rows",
        'Part/P-1 "Bearing" | category=bearing; unit_cost=10.79',
        '  supplied_by: Supplier/S-1 "Acme"',
        "next_cursor: c2",
    ]


async def test_search_is_lexical_unless_a_vector_is_given(
    connect: Connect, fake: FakeKeeper
) -> None:
    hits = {
        "revision": REVISION,
        "mode": "lexical",
        "items": [
            {"kind": "Part", "key": "P-1", "title": "Bearing", "snippet": "roller", "score": 0.5}
        ],
    }
    fake.on("POST", f"{API}/search", hits)
    async with connect() as client:
        out = text(await client.call_tool("lorebase_search", {"query": "bearing", "limit": 5}))
        assert fake.last_body() == {"q": "bearing", "mode": "lexical", "limit": 5}
        await client.call_tool("lorebase_search", {"query": "x", "query_vector": [0.1, 0.2]})
        assert fake.last_body() == {"q": "x", "query_vector": [0.1, 0.2]}
        missing = error_text(await client.call_tool("lorebase_search", {}))
    assert out.splitlines()[1] == '1. Part/P-1 "Bearing" (0.5) - roller'
    assert "[query-required]" in missing


async def test_neighborhood_renders_nodes_edges_and_truncation(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on(
        "POST",
        f"{API}/neighborhood",
        {
            "revision": REVISION,
            "nodes": [
                record("Supplier", "S-1", "Acme", tier="1"),
                record("Part", "P-1", "Bearing"),
            ],
            "edges": [
                {
                    "relation": "supplied_by",
                    "from": {"kind": "Part", "key": "P-1"},
                    "to": {"kind": "Supplier", "key": "S-1"},
                    "values": {},
                }
            ],
            "truncated": True,
        },
    )
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_neighborhood", {"seeds": ["Supplier/S-1"], "relations": ["supplies"]}
            )
        )
        too_deep = await client.call_tool(
            "lorebase_neighborhood", {"seeds": ["Supplier/S-1"], "depth": 9}
        )
    assert fake.last_body() == {
        "seeds": [{"kind": "Supplier", "key": "S-1"}],
        "depth": 1,
        "max_nodes": 50,
        "relations": ["supplies"],
    }
    assert 'Supplier/S-1 "Acme"' in out
    assert "tier=1" not in out
    assert "Part/P-1 -supplied_by-> Supplier/S-1" in out
    assert "truncated: Keeper caps reached" in out
    assert too_deep.is_error


def _context_routes(fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    fake.on("GET", f"{API}/kinds/Supplier/records/S-1001", SUPPLIER_RECORD)
    fake.on(
        "POST",
        f"{API}/neighborhood",
        {
            "revision": REVISION,
            "nodes": [
                record("Supplier", "S-1001", "Kavsen Alloys"),
                *(
                    record(
                        "PurchaseOrder", f"PO-{i}", f"Order {i}", total=f"{i}.00", note="x" * 200
                    )
                    for i in range(30)
                ),
            ],
            "edges": [
                {
                    "relation": "issued_to",
                    "from": {"kind": "PurchaseOrder", "key": f"PO-{i}"},
                    "to": {"kind": "Supplier", "key": "S-1001"},
                    "values": {},
                }
                for i in range(30)
            ],
            "truncated": False,
        },
    )


MARKDOWN_PACK = "# Context: Supplier/S-1001\nsupply/main at revision " + REVISION + "\n"


def _markdown(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, text=MARKDOWN_PACK, headers={"content-type": "text/markdown; charset=utf-8"}
    )


async def test_context_uses_keepers_context_endpoint(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("POST", f"{API}/context", _markdown)
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_context",
                {
                    "records": ["Supplier/S-1001"],
                    "token_budget": 800,
                    "depth": 2,
                    "relations": ["purchase_orders"],
                    "branch": "agent/a",
                },
            )
        )
    assert out == MARKDOWN_PACK
    assert [r.url.path for r in fake.requests] == [f"{API}/context"]
    assert fake.last().headers["Authorization"] == f"Bearer {AGENT_KEY}"
    assert fake.last_body() == {
        "refs": ["Supplier/S-1001"],
        "depth": 2,
        "budget_tokens": 800,
        "format": "markdown",
        "relations": ["purchase_orders"],
        "branch": "agent/a",
    }


async def test_context_endpoint_json_notes_seeds_found_by_search(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on(
        "POST",
        f"{API}/search",
        {"revision": REVISION, "items": [{"kind": "Supplier", "key": "S-1001", "score": 1.0}]},
    )
    fake.on("POST", f"{API}/context", {"revision": REVISION, "seeds": [{"ref": "Supplier/S-1001"}]})
    async with connect() as client:
        packed = json.loads(
            text(
                await client.call_tool("lorebase_context", {"question": "kavsen", "format": "json"})
            )
        )
        found = text(await client.call_tool("lorebase_context", {"question": "kavsen"}))
    assert packed == {
        "revision": REVISION,
        "seeds": [{"ref": "Supplier/S-1001"}],
        "found_by": "search",
        "question": "kavsen",
    }
    assert found.startswith("seeds found by search for 'kavsen'\n")
    context = [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/context")]
    assert context[0]["refs"] == ["Supplier/S-1001"]
    assert context[0]["format"] == "json"


async def test_context_endpoint_errors_are_not_masked_by_the_fallback(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on("POST", f"{API}/context", problem(404, "not-found", "Record Supplier/S-404 not found."))
    async with connect() as client:
        out = error_text(
            await client.call_tool("lorebase_context", {"records": ["Supplier/S-404"]})
        )
    assert "not-found" in out
    assert [r.url.path for r in fake.requests] == [f"{API}/context"]


async def test_context_composes_a_pack_within_the_budget(
    connect: Connect, fake: FakeKeeper
) -> None:
    _context_routes(fake)
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_context", {"records": ["Supplier/S-1001"], "token_budget": 400}
            )
        )
    assert out.startswith(f"context supply@main rev {REVISION}")
    assert fake.requests[0].url.path == f"{API}/context"  # missing: composed here
    assert 'SEEDS:\nSupplier/S-1001 "Kavsen Alloys"' in out
    assert 'PurchaseOrder/PO-0 "Order 0" -issued_to-> Supplier/S-1001' in out
    assert "[context trimmed to budget:" in out
    assert len(out) <= 400 * 4 + 200  # budget plus the closing marker


async def test_context_large_budget_includes_schema_and_neighbors(
    connect: Connect, fake: FakeKeeper
) -> None:
    _context_routes(fake)
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_context", {"records": ["Supplier/S-1001"], "token_budget": 20000}
            )
        )
        packed = json.loads(
            text(
                await client.call_tool(
                    "lorebase_context",
                    {"records": ["Supplier/S-1001"], "token_budget": 600, "format": "json"},
                )
            )
        )
    assert "SCHEMA (kinds involved):" in out
    assert "NEIGHBORS:" in out
    assert "trimmed" not in out
    assert packed["seeds"][0]["ref"] == "Supplier/S-1001"
    assert packed["trimmed"]["neighbors"] > 0


async def test_context_finds_seeds_by_question(connect: Connect, fake: FakeKeeper) -> None:
    _context_routes(fake)
    fake.on(
        "POST",
        f"{API}/search",
        {
            "revision": REVISION,
            "mode": "lexical",
            "items": [{"kind": "Supplier", "key": "S-1001", "title": "Kavsen", "score": 1.0}],
        },
    )
    async with connect() as client:
        out = text(await client.call_tool("lorebase_context", {"question": "kavsen alloys"}))
    assert "seeds found by search for 'kavsen alloys'" in out
    search = next(r for r in fake.requests if r.url.path.endswith("/search"))
    assert json.loads(search.content) == {"q": "kavsen alloys", "mode": "lexical", "limit": 3}


EXPLAINED = {
    "domain": "supply",
    "branch": "main",
    "revision": REVISION,
    "record": {
        "ref": "Supplier/S-1001",
        "kind": "Supplier",
        "key": "S-1001",
        "title": "Kavsen Alloys",
        "values": {"supplier_code": "S-1001", "tier": "1"},
        "updated_revision": REVISION,
    },
    "created": {
        "revision": PARENT,
        "at": "2026-10-01T08:00:03Z",
        "actor": "usr_loader",
        "origin_type": "import",
        "message": "Import the supply example data",
    },
    "attributes": [
        {
            "name": "supplier_code",
            "value": "S-1001",
            "revision": PARENT,
            "at": "2026-10-01T08:00:03Z",
            "actor": "usr_loader",
            "origin_type": "import",
        },
        {"name": "tier", "value": "1"},
    ],
    "links": [
        {
            "relation": "purchase_orders",
            "direction": "inverse",
            "ref": "PurchaseOrder/PO-1",
            "title": "PO-1",
            "revision": PARENT,
            "at": "2026-10-01T08:00:03Z",
            "actor": "usr_loader",
            "origin_type": "import",
        }
    ],
    "links_truncated": False,
    "history": {"changes": 2},
    "proposals": [
        {"id": "prp_1", "title": "Raise risk", "status": "merged", "merged_revision": REVISION}
    ],
}


async def test_explain_uses_keepers_explain_endpoint(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/kinds/Supplier/records/S-1001/explain", EXPLAINED)
    async with connect() as client:
        out = text(await client.call_tool("lorebase_explain", {"ref": "Supplier/S-1001"}))
        only = json.loads(
            text(
                await client.call_tool(
                    "lorebase_explain",
                    {"ref": "Supplier/S-1001", "attributes": ["tier"], "format": "json"},
                )
            )
        )
    assert fake.last().url.params["format"] == "json"
    assert {r.url.path for r in fake.requests} == {f"{API}/kinds/Supplier/records/S-1001/explain"}
    lines = out.splitlines()
    assert lines[0] == (
        f'Supplier/S-1001 "Kavsen Alloys" rev {REVISION} on main (2 changes, last in {REVISION})'
    )
    assert (
        f"supplier_code = S-1001 <- import by usr_loader at 2026-10-01T08:00:03Z in {PARENT}"
        in lines
    )
    assert "tier = 1 <- no recorded origin" in lines
    assert "LINKS:" in lines
    assert any(line.startswith('purchase_orders -> PurchaseOrder/PO-1 "PO-1"') for line in lines)
    assert f'prp_1 "Raise risk" merged in {REVISION}' in lines
    assert [a["name"] for a in only["attributes"]] == ["tier"]
    assert "links" not in only


async def test_explain_lists_lineage_per_attribute(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/kinds/Supplier/records/S-1001", SUPPLIER_RECORD)
    async with connect() as client:
        out = text(await client.call_tool("lorebase_explain", {"ref": "Supplier/S-1001"}))
    assert fake.requests[0].url.path.endswith("/explain")  # missing: read from the lineage
    assert fake.last().url.params["lineage"] == "true"
    assert "supplier_code = S-1001 <- import by usr_loader at 2026-10-01T08:00:03Z (pvn_1)" in out
    assert "tier = 1 <- no recorded origin" in out


def _log(request: httpx.Request) -> httpx.Response:
    entries = [
        {
            "id": rev,
            "created_at": "2026-10-02T00:00:00Z",
            "author": "usr_editor",
            "origin": {"type": "operation"},
            "message": f"change {rev[:2]}",
            "stats": {"records": 1},
        }
        for rev in (REVISION, PARENT, OLDER)
    ]
    if request.url.params.get("cursor") == "page2":
        return httpx.Response(200, json={"branch": "main", "head": REVISION, "items": entries[2:]})
    return httpx.Response(
        200,
        json={"branch": "main", "head": REVISION, "items": entries[:2], "next_cursor": "page2"},
    )


async def test_changes_walks_the_log_back_to_since(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", "/api/v1/domains/supply", {"id": "supply", "default_branch": "main"})
    fake.on("GET", f"{API}/branches/main/log", _log)
    async with connect() as client:
        out = text(await client.call_tool("lorebase_changes", {"since": OLDER[:10]}))
        latest = text(await client.call_tool("lorebase_changes", {"limit": 1}))
    lines = out.splitlines()
    assert lines[0] == f"main head {REVISION}: 2 Revisions since {OLDER[:10]}"
    assert lines[1].startswith(f"{REVISION} 2026-10-02T00:00:00Z usr_editor [operation]")
    assert "since not reached" not in out
    assert latest.splitlines()[0] == f"main head {REVISION}: 1 Revisions"


async def test_diff_renders_summary_and_value_changes(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "GET",
        f"{API}/diff",
        {
            "from_revision": PARENT,
            "to_revision": REVISION,
            "total": 2,
            "summary": [
                {
                    "partition": "k",
                    "element": "record",
                    "name": "Supplier",
                    "added": 1,
                    "modified": 1,
                    "removed": 0,
                }
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
                    "element": "link",
                    "change": "added",
                    "partition": "r",
                    "relation": "supplied_by",
                    "from_key": "P-1",
                    "to_key": "S-1",
                },
            ],
        },
    )
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_diff",
                {"from_revision": "v1", "to_revision": "main", "kinds": ["Supplier"]},
            )
        )
    params = fake.last().url.params
    assert (params["from"], params["to"], params.get_list("kinds")) == ("v1", "main", ["Supplier"])
    assert out.splitlines() == [
        f"diff {PARENT}..{REVISION}: 2 changes",
        "summary: Supplier +1 ~1 -0",
        "~ Supplier/S-1",
        "  city: Poznan -> Lodz",
        "+ supplied_by P-1 -> S-1",
    ]


async def test_branches_marks_default_and_protected(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "GET",
        f"{API}/branches",
        {
            "items": [
                {"name": "main", "head": REVISION, "default": True, "protected": True},
                {"name": "agent/x", "head": PARENT, "default": False, "protected": False},
            ]
        },
    )
    async with connect() as client:
        out = text(await client.call_tool("lorebase_branches", {}))
    assert out.splitlines() == [
        f"main (default, protected) head {REVISION}",
        f"agent/x head {PARENT}",
    ]


# Writes --------------------------------------------------------------------------------------


EXECUTION = {
    "status": "dry_run",
    "operation": "update_supplier",
    "branch": "main",
    "invocation_id": "inv_1",
    "parameters_hash": "h",
    "head": REVISION,
    "revision": None,
    "revisions": [],
    "edits": [{"op": "upsert", "kind": "Supplier", "key": "S-1"}],
    "edit_count": 1,
    "edits_truncated": False,
    "violations": [],
    "violation_count": 0,
    "warnings": [],
    "review_required": False,
    "replayed": False,
}


async def test_execute_dry_runs_by_default(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("POST", f"{API}/operations/update_supplier/execute", EXECUTION)
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_execute",
                {"operation": "update_supplier", "parameters": {"key": "S-1", "tier": "2"}},
            )
        )
    assert fake.last_body() == {"parameters": {"key": "S-1", "tier": "2"}, "dry_run": True}
    assert out.splitlines()[0] == (
        f"dry_run update_supplier on main (head {REVISION}): 1 edits, 0 violations"
    )
    assert f"next: call again with dry_run=false and expected_head={REVISION}" in out


async def test_execute_commit_carries_expected_head(connect: Connect, fake: FakeKeeper) -> None:
    committed = {**EXECUTION, "status": "committed", "revision": PARENT, "revisions": [PARENT]}
    fake.on("POST", f"{API}/operations/update_supplier/execute", committed)
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_execute",
                {
                    "operation": "update_supplier",
                    "parameters": {"key": "S-1"},
                    "dry_run": False,
                    "expected_head": REVISION,
                    "idempotency_key": "k-1",
                    "format": "json",
                },
            )
        )
    assert fake.last_body() == {
        "parameters": {"key": "S-1"},
        "dry_run": False,
        "expected_head": REVISION,
        "idempotency_key": "k-1",
    }
    assert json.loads(out)["revision"] == PARENT


PROPOSED = {
    "status": "proposed",
    "operation": "flag_supplier_risk",
    "branch": "agent/svc_1/audit",
    "target": "main",
    "invocation_id": "inv_1",
    "parameters_hash": "h",
    "revision": PARENT,
    "revisions": [PARENT],
    "edit_count": 2,
    "replayed": False,
    "proposal": {
        "id": "prp_1",
        "title": "Agent svc_1: audit",
        "status": "open",
        "source_ref": "agent/svc_1/audit",
        "target_ref": "main",
        "url": "/api/v1/domains/supply/proposals/prp_1",
        "created": True,
    },
    "approval": {"required": 1, "approvals": 0, "satisfied": False},
    "attempt": {"id": "mat_1", "open_conflicts": 0},
    "merge": {
        "requested": True,
        "allowed": True,
        "performed": False,
        "blockers": ["approvals-pending"],
    },
    "merged_revision": None,
    "warnings": [],
    "next": [
        {
            "action": "await_review",
            "detail": "Waiting for approval (0 of 1 required).",
            "method": "POST",
            "href": "/api/v1/domains/supply/proposals/prp_1/reviews",
        }
    ],
}


async def test_execute_with_a_task_reports_the_proposal(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("POST", f"{API}/operations/flag_supplier_risk/execute", PROPOSED)
    async with connect() as client:
        out = text(
            await client.call_tool(
                "lorebase_execute",
                {
                    "operation": "flag_supplier_risk",
                    "parameters": {"supplier": "S-1"},
                    "dry_run": False,
                    "task": "audit",
                    "intent": "Flag S-1 after the audit",
                    "auto_merge": True,
                },
            )
        )
    assert fake.last_body() == {
        "parameters": {"supplier": "S-1"},
        "dry_run": False,
        "task": "audit",
        "intent": "Flag S-1 after the audit",
        "auto_merge": True,
    }
    lines = out.splitlines()
    assert lines[0] == (
        "proposed flag_supplier_risk on agent/svc_1/audit -> main: Proposal prp_1 (open), 2 edits"
    )
    assert "approvals 0 of 1 required" in lines
    assert "auto_merge requested, not performed: approvals-pending" in lines
    assert lines[-1].startswith("next: await_review: Waiting for approval")
    assert lines[-1].endswith("[POST /api/v1/domains/supply/proposals/prp_1/reviews]")


# Size caps -----------------------------------------------------------------------------------


async def test_outputs_are_capped_with_markers(connect: Connect, fake: FakeKeeper) -> None:
    items = [record("Part", f"P-{i}", "x" * 50, summary="y" * 400) for i in range(80)]
    fake.on("POST", f"{API}/query", {"revision": REVISION, "items": items})
    async with connect(output={"max_items": 10, "max_value_chars": 20, "max_chars": 900}) as c:
        out = text(await c.call_tool("lorebase_query", {"query": {"kind": "Part"}}))
    assert len(out) <= 900
    assert "summary=yyyyyyyyyyyyyyyyyyy…" in out
    assert out.rstrip().endswith("or follow `next_cursor`]")
    async with connect(output={"max_items": 10}) as c:
        listed = text(await c.call_tool("lorebase_query", {"query": {"kind": "Part"}}))
    assert "... 70 more (lower `limit`)" in listed


@pytest.mark.parametrize("fmt", ["text", "json"])
async def test_resources_and_prompt(connect: Connect, fake: FakeKeeper, fmt: str) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    fake.on("GET", f"{API}/kinds/Supplier/records/S-1001", SUPPLIER_RECORD)
    async with connect(output={"default_format": fmt}) as client:
        templates = await client.list_resource_templates()
        schema = await client.read_resource("lorebase://supply/schema")
        found = await client.read_resource("lorebase://supply/Supplier/S-1001")
        prompt = await client.get_prompt("lorebase_guide", {"task": "check S-1001"})
    assert {t.uri_template for t in templates.resource_templates} == {
        "lorebase://{domain}/schema",
        "lorebase://{domain}/{kind}/{key}",
    }
    assert schema.contents[0].text.startswith("schema supply@main")  # type: ignore[union-attr]
    assert 'Supplier/S-1001 "Kavsen Alloys"' in found.contents[0].text  # type: ignore[union-attr]
    guide = prompt.messages[0].content.text  # type: ignore[union-attr]
    assert "lorebase_context" in guide
    assert "Task: check S-1001" in guide
