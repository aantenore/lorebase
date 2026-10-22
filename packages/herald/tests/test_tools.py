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
    CONTEXT_PACK,
    EXECUTION,
    EXPLAINED,
    HISTORY,
    LINEAGE,
    MARKDOWN_PACK,
    OLDER,
    PARENT,
    REVISION,
    SCHEMA,
    SUPPLIER_RECORD,
    FakeKeeper,
    branch_entry,
    markdown_pack,
    problem,
    record,
    revision_log,
    served,
)

TOOLS = {
    "lorebase_schema",
    "lorebase_get",
    "lorebase_query",
    "lorebase_search",
    "lorebase_neighborhood",
    "lorebase_context",
    "lorebase_explain",
    "lorebase_history",
    "lorebase_lineage",
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
    assert "required for the proposal flow" in description
    assert "problem text" in description
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
    assert "SEARCH modes lexical, semantic, hybrid" in out
    assert (
        "query_vector of 384 dimensions made by the embedding model BAAI/bge-small-en-v1.5" in out
    )
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
    assert view["search"]["embedding"] == {"model": "BAAI/bge-small-en-v1.5", "dims": 384}
    assert fake.last().url.params["branch"] == "dev"


async def test_schema_without_embedding_lists_only_lexical_search(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on("GET", f"{API}/schema", {**SCHEMA, "search": {"modes": ["lexical"], "embedding": None}})
    async with connect() as client:
        out = text(await client.call_tool("lorebase_schema", {}))
    assert out.splitlines()[-1] == "SEARCH modes lexical"


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
        served(record=record("Doc", "a b", "A")),
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
            "served_from": "materialized",
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
        "served_from": "materialized",
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
            "served_from": "materialized",
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


async def test_context_uses_keepers_context_endpoint(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("POST", f"{API}/context", markdown_pack)
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
        "include_schema": True,
        "include_recent_changes": True,
        "include_lineage": True,
        "relations": ["purchase_orders"],
        "branch": "agent/a",
    }


async def test_context_endpoint_json_notes_seeds_found_by_search(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on(
        "POST",
        f"{API}/search",
        served(mode="lexical", items=[{"kind": "Supplier", "key": "S-1001", "score": 1.0}]),
    )
    fake.on("POST", f"{API}/context", CONTEXT_PACK)
    async with connect() as client:
        packed = json.loads(
            text(
                await client.call_tool("lorebase_context", {"question": "kavsen", "format": "json"})
            )
        )
        found = text(await client.call_tool("lorebase_context", {"question": "kavsen"}))
    assert packed == {**CONTEXT_PACK, "found_by": "search", "question": "kavsen"}
    assert found.startswith("seeds found by search for 'kavsen'\n")
    context = [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/context")]
    assert context[0]["refs"] == ["Supplier/S-1001"]
    assert context[0]["format"] == "json"


async def test_context_endpoint_errors_are_tool_errors(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("POST", f"{API}/context", problem(404, "not-found", "Record Supplier/S-404 not found."))
    async with connect() as client:
        out = error_text(
            await client.call_tool("lorebase_context", {"records": ["Supplier/S-404"]})
        )
    assert "not-found" in out
    assert [r.url.path for r in fake.requests] == [f"{API}/context"]


async def test_context_finds_seeds_by_question(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("POST", f"{API}/context", markdown_pack)
    fake.on(
        "POST",
        f"{API}/search",
        served(
            mode="lexical",
            items=[{"kind": "Supplier", "key": "S-1001", "title": "Kavsen", "score": 1.0}],
        ),
    )
    async with connect() as client:
        out = text(await client.call_tool("lorebase_context", {"question": "kavsen alloys"}))
    assert "seeds found by search for 'kavsen alloys'" in out
    search = next(r for r in fake.requests if r.url.path.endswith("/search"))
    assert json.loads(search.content) == {"q": "kavsen alloys", "mode": "lexical", "limit": 3}


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


async def test_history_and_lineage_use_keepers_record_endpoints(
    connect: Connect, fake: FakeKeeper
) -> None:
    base = f"{API}/kinds/Supplier/records/S-1001"
    fake.on("GET", f"{base}/history", HISTORY)
    fake.on("GET", f"{base}/lineage", LINEAGE)
    async with connect() as client:
        history = text(
            await client.call_tool(
                "lorebase_history",
                {"ref": "Supplier/S-1001", "values": True, "limit": 5, "cursor": "h1"},
            )
        )
        lineage = text(await client.call_tool("lorebase_lineage", {"ref": "Supplier/S-1001"}))
    params = next(r for r in fake.requests if r.url.path.endswith("/history")).url.params
    assert (params["values"], params["limit"], params["cursor"]) == ("true", "5", "h1")
    assert "  tier: null -> 2" in history.splitlines()
    assert history.splitlines()[-1] == "next_cursor: h2"
    assert "tier = 1 <- extraction by usr_loader" in lineage
    assert "extracted from document doc_1 [10:24] by m-1" in lineage
    assert "SOURCES: erp" in lineage


async def test_changes_walks_the_log_back_to_since(connect: Connect, fake: FakeKeeper) -> None:
    fake.on(
        "GET",
        "/api/v1/domains/supply",
        {
            "id": "supply",
            "display_name": "Supply",
            "default_branch": "main",
            "created_at": "2026-10-01T08:00:00Z",
            "created_by": "usr_loader",
        },
    )
    fake.on("GET", f"{API}/branches/main/log", revision_log)
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
                branch_entry("main", REVISION, default=True, protected=True),
                branch_entry("agent/x", PARENT, default=False, protected=False),
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
    }
    assert fake.last().headers["idempotency-key"] == "k-1"
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
    "attempt": {
        "id": "mat_1",
        "result": None,
        "open_conflicts": 0,
        "resolved_conflicts": 0,
        "counts": {},
    },
    "merge": {
        "requested": True,
        "allowed": True,
        "performed": False,
        "blockers": ["approvals-pending"],
    },
    "merged_revision": None,
    "branch_created": True,
    "branch_deleted": False,
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
    fake.on(
        "POST",
        f"{API}/operations/flag_supplier_risk/execute",
        httpx.Response(202, json=PROPOSED),
    )
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


async def test_execute_without_a_task_passes_auto_propose_to_keeper_and_returns_its_problem(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on(
        "POST",
        f"{API}/operations/flag_supplier_risk/execute",
        problem(
            422,
            "validation-failed",
            "One or more request members are invalid.",
            errors=[{"pointer": "/body/task", "message": "is required with auto_propose"}],
        ),
    )
    async with connect() as client:
        out = error_text(
            await client.call_tool(
                "lorebase_execute",
                {
                    "operation": "flag_supplier_risk",
                    "dry_run": False,
                    "intent": "Flag S-1",
                    "auto_propose": True,
                },
            )
        )
    assert fake.last_body() == {
        "parameters": {},
        "dry_run": False,
        "intent": "Flag S-1",
        "auto_propose": True,
    }
    assert "[validation-failed] One or more request members are invalid." in out
    assert "/body/task: is required with auto_propose" in out


async def test_execute_with_only_intent_still_goes_through_keeper_validation(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on(
        "POST",
        f"{API}/operations/flag_supplier_risk/execute",
        problem(409, "review-required", "Edits need review.", proposals="/proposals"),
    )
    async with connect() as client:
        out = error_text(
            await client.call_tool(
                "lorebase_execute",
                {"operation": "flag_supplier_risk", "dry_run": False, "intent": "Flag S-1"},
            )
        )
    assert fake.last_body() == {"parameters": {}, "dry_run": False, "intent": "Flag S-1"}
    assert "[review-required] Edits need review." in out


# Size caps -----------------------------------------------------------------------------------


async def test_outputs_are_capped_with_markers(connect: Connect, fake: FakeKeeper) -> None:
    items = [record("Part", f"P-{i}", "x" * 50, summary="y" * 400) for i in range(80)]
    fake.on("POST", f"{API}/query", served(items=items))
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
