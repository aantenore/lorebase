"""The client against a real Keeper (in-process through `httpx.ASGITransport`) on Postgres.

Covers the full flow on the `supply` example: Domain, schema, import, branch, change, read,
query, diff and log, plus errors, idempotency, the ETag cache and API key administration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from keeper_harness import RunningKeeper
from supply_example import (
    DOMAIN,
    EXAMPLE,
    WORK,
    Loaded,
    attribute_types,
    read_rows,
    stripped_schema_yaml,
)

from lorebase_client import (
    AsyncLorebaseClient,
    AuthenticationError,
    LorebaseClient,
    NotFoundError,
    PermissionDeniedError,
    ProtectedBranchError,
    StaleHeadError,
    ValidationFailedError,
)
from lorebase_client.models import QueryRequest

pytestmark = pytest.mark.integration

TIMEOUT_DOC = Path(__file__).resolve().parents[4] / "docs" / "openapi.json"


async def test_domain_branch_and_schema_lifecycle(client: AsyncLorebaseClient) -> None:
    me = await client.whoami()
    assert me.installation_admin

    created = await client.domains.create(DOMAIN, display_name="Supply chain")
    assert created.id == DOMAIN
    assert created.default_branch == "main"
    detail = await client.domains.get(DOMAIN)
    assert detail.head is not None
    assert [d.id for d in (await client.domains.list()).items] == [DOMAIN]
    renamed = await client.domains.update(DOMAIN, description="Parts and suppliers")
    assert renamed.description == "Parts and suppliers"

    main = await client.branches.get(DOMAIN, "main")
    assert main.protected
    assert main.default
    with pytest.raises(ProtectedBranchError) as protected:
        await client.schema.apply(DOMAIN, "main", yaml=stripped_schema_yaml())
    assert protected.value.extra["branch"] == "main"

    work = await client.branches.create(DOMAIN, WORK)
    assert work.head == main.head
    assert not work.protected
    nested = await client.branches.create(DOMAIN, "agent/bot-1/task", source=WORK)
    assert (await client.branches.get(DOMAIN, nested.name)).name == "agent/bot-1/task"
    names = {b.name async for b in client.branches.iterate(DOMAIN)}
    assert names == {"main", WORK, "agent/bot-1/task"}

    # The example as shipped holds CEL Operations and Derivations, type checked on validation.
    shipped = await client.schema.validate(DOMAIN, yaml=(EXAMPLE / "schema.yaml").read_text())
    assert shipped.valid, shipped.issues

    stripped = stripped_schema_yaml()
    checked = await client.schema.validate(DOMAIN, yaml=stripped, branch=WORK)
    assert checked.valid
    assert checked.changes
    dry = await client.schema.apply(DOMAIN, WORK, yaml=stripped, dry_run=True)
    assert dry.status == "dry_run"
    assert dry.revision is None
    with pytest.raises(StaleHeadError) as stale:
        await client.schema.apply(DOMAIN, WORK, yaml=stripped, expected_head="00" * 32)
    assert stale.value.current_head == work.head
    applied = await client.schema.apply(DOMAIN, WORK, yaml=stripped, message="Apply the schema")
    assert applied.status == "committed"
    assert applied.change_count == 18

    document = await client.schema.get(DOMAIN, branch=WORK)
    assert {k["api_name"] for k in document.schema_["kinds"].values()} >= {"Supplier", "Part"}
    assert "Supplier:" in await client.schema.get_yaml(DOMAIN, branch=WORK)
    element = await client.schema.element(DOMAIN, "kinds", "Supplier", branch=WORK)
    assert element["api_name"] == "Supplier"
    builtin = await client.schema.element(DOMAIN, "operations", "import_records", branch=WORK)
    assert builtin["name" if "name" in builtin else "api_name"] == "import_records"

    tagged = await client.tags.create(DOMAIN, "v1", source=WORK)
    assert tagged.revision == applied.revision
    assert [t.name for t in (await client.tags.list(DOMAIN)).items] == ["v1"]

    flipped = await client.branches.set_protected(DOMAIN, WORK, True)
    assert flipped.protected
    await client.branches.set_protected(DOMAIN, WORK, False)
    await client.branches.delete(DOMAIN, "agent/bot-1/task")
    with pytest.raises(NotFoundError):
        await client.branches.get(DOMAIN, "agent/bot-1/task")


async def test_imports_validate_replay_and_report_issues(
    client: AsyncLorebaseClient, supply: Loaded
) -> None:
    assert supply.counts["Supplier"] == 30
    head = (await client.branches.get(DOMAIN, WORK)).head

    bad = [{"supplier_code": "not-a-code", "legal_name": "X", "country": "PL"}]
    with pytest.raises(ValidationFailedError) as invalid:
        await client.operations.import_records(DOMAIN, "Supplier", bad, branch=WORK)
    assert invalid.value.issues
    assert invalid.value.issues[0]["severity"] == "error"
    assert (await client.branches.get(DOMAIN, WORK)).head == head  # nothing was written

    rows = [{"supplier_code": "S-9001", "legal_name": "Idempotent Ltd", "country": "GB"}]
    key = client.idempotency_key("import")
    first = await client.operations.import_records(
        DOMAIN, "Supplier", rows, branch=WORK, idempotency_key=key
    )
    again = await client.operations.import_records(
        DOMAIN, "Supplier", rows, branch=WORK, idempotency_key=key
    )
    assert first.status == "committed"
    assert not first.replayed
    assert again.replayed
    assert again.revisions == first.revisions

    preview = await client.operations.import_records(
        DOMAIN, "Supplier", rows, branch=WORK, dry_run=True
    )
    assert preview.status == "dry_run"
    assert preview.revision is None


async def test_reads_queries_and_the_etag_cache(
    client: AsyncLorebaseClient, supply: Loaded
) -> None:
    got = await client.records.get(DOMAIN, "Supplier", "S-1001", branch=WORK, links=True)
    assert got.served_from == "materialized"
    assert got.record.title == "Kavsen Alloys Sp. z o.o."
    assert got.record.values["risk_score"] == "32"  # integers travel as strings
    assert got.record.links
    again = await client.records.get(DOMAIN, "Supplier", "S-1001", branch=WORK, links=True)
    assert again == got  # answered from the cache after a 304
    assert client._core.cache is not None
    assert len(client._core.cache) >= 1

    page = await client.records.list(DOMAIN, "Supplier", branch=WORK, limit=7, order_by=["key"])
    assert len(page.items) == 7
    assert page.next_cursor
    everyone = [
        r.key async for r in client.records.iterate(DOMAIN, "Supplier", branch=WORK, page_size=7)
    ]
    assert len(everyone) == supply.counts["Supplier"]
    assert everyone == sorted(everyone)
    polish = [
        r.key
        async for r in client.records.iterate(
            DOMAIN,
            "Supplier",
            branch=WORK,
            where={"attr": "country", "op": "eq", "value": "PL"},
            select=["key", "country"],
        )
    ]
    assert polish
    assert all(key.startswith("S-") for key in polish)

    links = await client.records.links(DOMAIN, "Part", "P-10001", "supplied_by", branch=WORK)
    assert links.items
    assert links.items[0].values["is_primary"] is True
    assert links.items[0].target is not None
    relation = [
        link
        async for link in client.records.iterate_relation_links(
            DOMAIN, "supplied_by", branch=WORK, page_size=10
        )
    ]
    assert len(relation) == supply.counts["supplied_by"]

    result = await client.records.query(
        DOMAIN,
        QueryRequest(
            kind="Part",
            where={"attr": "category", "op": "eq", "value": "bearing"},
            aggregate={"group_by": "lifecycle"},
            branch=WORK,
        ),
    )
    assert result.aggregate is not None
    assert result.aggregate.counts
    streamed = [
        r.key
        async for r in client.records.iterate_query(
            DOMAIN, {"kind": "Part", "limit": 15, "branch": WORK}
        )
    ]
    assert len(streamed) == supply.counts["Part"]

    walked = await client.records.traverse(
        DOMAIN,
        {
            "branch": WORK,
            "start": {"kind": "Part", "key": "P-10001"},
            "path": [{"relation": "supplied_by"}],
        },
    )
    assert walked.start.key == "P-10001"
    assert walked.hops[0].records
    around = await client.records.neighborhood(
        DOMAIN, {"branch": WORK, "seeds": [{"kind": "Part", "key": "P-10001"}], "depth": 1}
    )
    assert len(around.nodes) > 1
    assert around.edges
    found = await client.records.search(DOMAIN, {"branch": WORK, "q": "bearing", "kinds": ["Part"]})
    assert found.items
    assert found.items[0].kind == "Part"

    with pytest.raises(NotFoundError):
        await client.records.get(DOMAIN, "Supplier", "S-0000", branch=WORK)
    with pytest.raises(ValidationFailedError) as bad_query:
        await client.records.query(DOMAIN, {"kind": "Part", "where": {"attr": "nope", "op": "eq"}})
    assert bad_query.value.errors


async def test_branching_change_diff_and_log(client: AsyncLorebaseClient, supply: Loaded) -> None:
    review = await client.branches.create(DOMAIN, "review", source=WORK)
    types = attribute_types(
        (await client.schema.get(DOMAIN, branch=WORK)).schema_, "kinds", "Supplier"
    )
    row = next(
        r for r in read_rows("supplier.csv", types, links=False) if r["supplier_code"] == "S-1001"
    )
    row["legal_name"] = "Kavsen Alloys Renamed"
    changed = await client.operations.import_records(
        DOMAIN, "Supplier", [row], branch="review", message="Rename S-1001"
    )
    assert changed.status == "committed"
    detail = await client.branches.get(DOMAIN, "review")
    assert (detail.ahead, detail.behind) == (len(supply.import_revisions) + 2, 0)
    assert (await client.records.get(DOMAIN, "Supplier", "S-1001", branch=WORK)).record.title == (
        "Kavsen Alloys Sp. z o.o."
    )
    assert (
        await client.records.get(DOMAIN, "Supplier", "S-1001", branch="review")
    ).record.title == ("Kavsen Alloys Renamed")
    earlier = await client.records.get(
        DOMAIN, "Supplier", "S-1001", branch="review", at=review.head
    )
    assert earlier.served_from == "tree"
    assert earlier.record.title == "Kavsen Alloys Sp. z o.o."

    diff = await client.revisions.diff(DOMAIN, WORK, "review")
    assert diff.total == 1
    assert diff.items[0].kind == "Supplier"
    assert diff.items[0].key == "S-1001"
    assert diff.items[0].changes is not None
    assert {c.name for c in diff.items[0].changes} == {"legal_name"}
    assert [
        c.key
        async for c in client.revisions.iterate_diff(DOMAIN, WORK, "review", kinds=["Supplier"])
    ] == ["S-1001"]

    log = await client.revisions.log(DOMAIN, "review", limit=2)
    assert log.items[0].message == "Rename S-1001"
    assert log.next_cursor
    history = [r async for r in client.revisions.iterate_log(DOMAIN, "review", page_size=3)]
    assert history[0].id == changed.revisions[-1]
    assert len(history) > len(supply.import_revisions)
    assert history == sorted(history, key=lambda r: -r.generation)

    revision = await client.revisions.get(DOMAIN, "review")
    assert revision.id == history[0].id
    assert revision.parents == [history[1].id]
    counts = await client.revisions.counts(DOMAIN, "review")
    assert {p.name: p.count for p in counts.partitions}["Supplier"] == supply.counts["Supplier"]
    changeset = [c async for c in client.revisions.iterate_changeset(DOMAIN, revision.id)]
    assert len(changeset) == 1
    assert changeset[0].provenance

    await client.branches.delete(DOMAIN, "review")

    # A deleted Domain is gone: an installation admin is told so.
    await client.domains.create("scratch")
    await client.domains.delete("scratch", confirm="scratch")
    with pytest.raises(NotFoundError):
        await client.domains.get("scratch")


async def test_proposal_merge_and_agent_reads(client: AsyncLorebaseClient, supply: Loaded) -> None:
    await client.branches.create(DOMAIN, "fix", source=WORK)
    types = attribute_types(
        (await client.schema.get(DOMAIN, branch=WORK)).schema_, "kinds", "Supplier"
    )
    row = next(
        r for r in read_rows("supplier.csv", types, links=False) if r["supplier_code"] == "S-1001"
    )
    row["legal_name"] = "Kavsen Alloys Fixed"
    await client.operations.import_records(DOMAIN, "Supplier", [row], branch="fix")

    proposal = await client.proposals.create(
        DOMAIN, "fix", WORK, title="Fix S-1001", idempotency_key=client.idempotency_key("p")
    )
    assert proposal.status == "open"
    assert [p.id async for p in client.proposals.iterate(DOMAIN, source="fix")] == [proposal.id]
    assert (await client.proposals.get(DOMAIN, proposal.id)).title == "Fix S-1001"

    merged = await client.proposals.merge(DOMAIN, proposal.id, message="Merge the fix")
    assert merged.proposal.status == "merged"
    assert (await client.records.get(DOMAIN, "Supplier", "S-1001", branch=WORK)).record.title == (
        "Kavsen Alloys Fixed"
    )

    pack = await client.agents.context(DOMAIN, {"branch": WORK, "refs": ["Supplier/S-1001"]})
    assert [seed["ref"] for seed in pack.seeds] == ["Supplier/S-1001"]
    explained = await client.agents.explain(DOMAIN, "Supplier", "S-1001", branch=WORK)
    assert explained.revision == merged.revision
    feed = await client.agents.changes(DOMAIN, WORK, since=supply.schema_revision)
    assert feed.head == merged.revision
    assert feed.items


async def test_api_keys_grants_and_audit(
    client: AsyncLorebaseClient, in_process_keeper: RunningKeeper, supply: Loaded
) -> None:
    created = await client.api_keys.create(DOMAIN, "viewer bot", role="viewer", ttl_days=1)
    assert created.key.startswith("lbk_")
    assert created.role == "viewer"
    listed = [k async for k in client.api_keys.iterate(DOMAIN)]
    assert [k.id for k in listed] == [created.id]
    assert "key" not in json.loads(listed[0].model_dump_json())

    async with AsyncLorebaseClient(
        in_process_keeper.url, api_key=created.key, transport=in_process_keeper.transport
    ) as reader:
        assert (await reader.whoami()).method == "api_key"
        assert (await reader.records.get(DOMAIN, "Supplier", "S-1001", branch=WORK)).record.key
        with pytest.raises(PermissionDeniedError) as denied:
            await reader.branches.create(DOMAIN, "nope")
        assert denied.value.extra["permission"] == "branch.create"
        assert denied.value.status == 403

    grants = [g async for g in client.grants.iterate(DOMAIN)]
    assert any(g.role == "viewer" for g in grants)
    mine = await client.grants.create(
        DOMAIN, created.principal_id, "editor", scope_type="branch", scope_ref="agent/*"
    )
    assert mine.scope_ref == "agent/*"
    await client.grants.revoke(DOMAIN, mine.id)
    assert [p.id async for p in client.principals.iterate(type_="service")]
    actions = {e.action async for e in client.audit.iterate(DOMAIN)}
    assert {"apikey.create", "grant.create", "branch.create"} <= actions

    await client.api_keys.revoke(DOMAIN, created.id)
    async with AsyncLorebaseClient(
        in_process_keeper.url, api_key=created.key, transport=in_process_keeper.transport
    ) as revoked:
        with pytest.raises(AuthenticationError):
            await revoked.whoami()
    async with AsyncLorebaseClient(
        in_process_keeper.url, transport=in_process_keeper.transport
    ) as anonymous:
        with pytest.raises(AuthenticationError):
            await anonymous.whoami()


async def test_models_match_the_live_openapi_document(
    in_process_keeper: RunningKeeper,
) -> None:
    """The document Keeper serves is the one the client models were checked against."""
    async with httpx.AsyncClient(
        transport=in_process_keeper.transport, base_url=in_process_keeper.url
    ) as http:
        live: dict[str, Any] = (await http.get("/api/v1/openapi.json")).json()
    committed = json.loads(TIMEOUT_DOC.read_text())
    assert live["components"]["schemas"] == committed["components"]["schemas"]
    assert live["paths"].keys() == committed["paths"].keys()


def test_sync_client_over_real_http(served_keeper: RunningKeeper) -> None:
    with LorebaseClient(served_keeper.url, api_key=served_keeper.admin_key, cache=True) as client:
        assert client.whoami().installation_admin
        client.domains.create("tiny", display_name="Tiny")
        client.branches.create("tiny", "work")
        applied = client.schema.apply("tiny", "work", yaml=_tiny_schema())
        assert applied.status == "committed"
        client.operations.import_records(
            "tiny", "Note", [{"title": "first"}, {"title": "second"}], branch="work"
        )
        keys = [r.title for r in client.records.iterate("tiny", "Note", branch="work", page_size=1)]
        assert sorted(keys) == ["first", "second"]
        assert [d.id for d in client.domains.iterate()] == ["tiny"]
        assert client.revisions.diff("tiny", "main", "work").total >= 1
        with pytest.raises(NotFoundError):
            client.domains.get("missing")
        client.domains.create("gone")
        client.domains.delete("gone", confirm="gone")


def _tiny_schema() -> str:
    return (
        "format: lorebase.schema/v1\n"
        "domain: {id: tiny}\n"
        "kinds:\n"
        "  Note:\n"
        "    id: knd_01KEZF7C007ZDX8SK1DZP7M001\n"
        "    description: A short note.\n"
        "    title_attribute: title\n"
        "    attributes:\n"
        "      title:\n"
        "        id: att_01KEZF7C00HDZZHMNS4181S001\n"
        "        description: What the note says.\n"
        "        type: string\n"
        "        required: true\n"
    )
