"""Reads are authorized against the branch, Revision and Kinds actually read.

A grant narrowed to `feature/*` must not read `main` data by naming a `main`-only Revision
through `at`, a tag, a Revision route or a diff side. A grant narrowed to the Kind `Supplier`
must not read other Kinds through links, Relation predicates, traversals, neighborhoods or
search.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.materialize import create_branch
from lorebase_store.models.enums import GrantScope, PrincipalType, RefKind
from lorebase_store.repositories import RefRepository
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PEPPER = "read-auth-pepper-0123456789-0123456789"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
BRANCH = "feature/x"


async def _principal(store: SqlAuthStore, name: str, **grant: Any) -> str:
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        await unit.grants.add(
            domain_id=DOMAIN, subject=principal.id, role="viewer", created_by="usr_seed", **grant
        )
        generated = CODEC.generate()
        await unit.api_keys.add(
            principal_id=principal.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=[f"domain:{DOMAIN}"],
        )
    return generated.token


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, (_, schema_commit, data) = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    await create_branch(
        session, DOMAIN, BRANCH, created_by="usr_seed", runtime=runtime, clock=clock
    )
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1002")
    main_only = await commit_changes(
        session,
        runtime,
        clock,
        [record_change(supplier, {**original, "city": "Secret City"})],
        message="Main-only change",
    )
    feature = await commit_changes(
        session,
        runtime,
        clock,
        [
            record_change(
                supplier,
                {"supplier_code": "S-9998", "legal_name": "Feature Only", "country": "DE"},
            )
        ],
        message="Feature-only change",
        branch=BRANCH,
    )
    async with session.begin():
        await RefRepository(session, DOMAIN).create(
            "audit-main",
            main_only.revision_id,
            created_at=datetime(2026, 10, 21, tzinfo=UTC),
            created_by="usr_seed",
            kind=RefKind.TAG,
        )
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        branch_key = await _principal(
            store, "branch reader", scope_type=GrantScope.BRANCH, scope_ref="feature/*"
        )
        domain_key = await _principal(store, "domain reader")
        kind_key = await _principal(
            store, "supplier reader", scope_type=GrantScope.KIND, scope_ref="Supplier"
        )
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.revisions = {
        "schema": schema_commit.revision_id.hex(),
        "data": data.revision_id.hex(),
        "main_only": main_only.revision_id.hex(),
        "feature": feature.revision_id.hex(),
    }
    seeded.facts = {"branch_key": branch_key, "domain_key": domain_key, "kind_key": kind_key}
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def _auth_section(**reads: Any) -> dict[str, Any]:
    section: dict[str, Any] = {"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}}}
    if reads:
        section["reads"] = reads
    return section


async def _status(http: httpx.AsyncClient, key: str, path: str, **params: Any) -> int:
    response = await http.get(
        BASE + path, params=params, headers={"Authorization": f"Bearer {key}"}
    )
    return response.status_code


async def test_branch_grant_cannot_read_other_branches_through_revisions(
    seeded: Seeded,
) -> None:
    key = seeded.facts["branch_key"]
    revs = seeded.revisions
    record = "/kinds/Supplier/records/S-1002"
    async with keeper(seeded.app_url, _auth_section()) as http:
        # its own branch, head and ancestors reachable from it
        assert await _status(http, key, record, branch=BRANCH) == 200
        assert await _status(http, key, record, branch=BRANCH, at=revs["data"]) == 200
        assert await _status(http, key, record, branch=BRANCH, at=BRANCH) == 200
        assert await _status(http, key, f"/branches/{BRANCH}/log") == 200
        assert await _status(http, key, f"/revisions/{revs['feature']}", branch=BRANCH) == 200
        assert await _status(http, key, f"/revisions/{revs['data']}", branch=BRANCH) == 200
        # a Revision reachable from a granted branch needs no branch to be named
        assert await _status(http, key, f"/revisions/{revs['data']}/counts") == 200
        assert await _status(http, key, f"/revisions/{revs['data']}") == 200
        assert (
            await _status(
                http, key, "/diff", **{"from": revs["data"], "to": BRANCH, "branch": BRANCH}
            )
            == 200
        )

        # main directly
        assert await _status(http, key, record) == 403
        assert await _status(http, key, record, branch="main") == 403
        assert await _status(http, key, "/branches/main/log") == 403
        # main through `at`: a main-only Revision id, the main branch name, a tag on main
        assert await _status(http, key, record, branch=BRANCH, at=revs["main_only"]) == 403
        assert await _status(http, key, record, branch=BRANCH, at="main") == 403
        assert await _status(http, key, record, branch=BRANCH, at="audit-main") == 403
        assert (
            await _status(http, key, "/kinds/Supplier/records", branch=BRANCH, at="audit-main")
            == 403
        )
        # Revision routes and diffs naming main-only Revisions
        assert await _status(http, key, f"/revisions/{revs['main_only']}") == 403
        assert await _status(http, key, f"/revisions/{revs['main_only']}", branch=BRANCH) == 403
        assert await _status(http, key, "/revisions/audit-main/changeset", branch=BRANCH) == 403
        assert await _status(http, key, f"/revisions/{revs['main_only']}/counts") == 403
        assert (
            await _status(
                http,
                key,
                "/diff",
                **{"from": revs["data"], "to": revs["main_only"], "branch": BRANCH},
            )
            == 403
        )
        assert await _status(http, key, "/diff", **{"from": BRANCH, "to": "main"}) == 403
        # unknown names do not reveal whether they exist to callers who could not read them
        assert await _status(http, key, "/revisions/nope") == 403
        searched = await http.post(
            f"{BASE}/search",
            json={"q": "Secret", "branch": "main"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert searched.status_code == 403


async def test_domain_grant_reads_any_revision(seeded: Seeded) -> None:
    key = seeded.facts["domain_key"]
    revs = seeded.revisions
    async with keeper(seeded.app_url, _auth_section()) as http:
        at_main = await http.get(
            f"{BASE}/kinds/Supplier/records/S-1002",
            params={"branch": BRANCH, "at": revs["main_only"]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert at_main.status_code == 200
        assert at_main.json()["record"]["values"]["city"] == "Secret City"
        assert await _status(http, key, f"/revisions/{revs['main_only']}") == 200
        assert await _status(http, key, "/revisions/nope") == 404


async def test_reachability_walk_is_bounded(seeded: Seeded) -> None:
    key = seeded.facts["branch_key"]
    async with keeper(seeded.app_url, _auth_section(reachability_max_depth=1)) as http:
        # the schema Revision is two parent steps below the feature head: beyond the bound
        status = await _status(
            http,
            key,
            "/kinds/Supplier/records/S-1002",
            branch=BRANCH,
            at=seeded.revisions["schema"],
        )
    assert status == 403


async def _post(
    http: httpx.AsyncClient, key: str, path: str, body: dict[str, Any]
) -> httpx.Response:
    return await http.post(BASE + path, json=body, headers={"Authorization": f"Bearer {key}"})


async def test_kind_grant_reads_only_its_kind(seeded: Seeded) -> None:
    key = seeded.facts["kind_key"]
    domain_key = seeded.facts["domain_key"]
    revs = seeded.revisions
    async with keeper(seeded.app_url, _auth_section()) as http:
        # its Kind, on any branch and at any Revision
        assert await _status(http, key, "/kinds/Supplier/records") == 200
        assert await _status(http, key, "/kinds/Supplier/records/S-1002") == 200
        assert await _status(http, key, "/kinds/Supplier/records", branch=BRANCH) == 200
        assert (
            await _status(http, key, "/kinds/Supplier/records/S-1002", at=revs["main_only"]) == 200
        )
        listed = await _post(http, key, "/query", {"kind": "Supplier", "limit": 5})
        assert listed.status_code == 200
        assert {i["kind"] for i in listed.json()["items"]} == {"Supplier"}

        # other Kinds directly
        assert await _status(http, key, "/kinds/Part/records") == 403
        assert await _status(http, key, "/kinds/Part/records/P-10001") == 403
        assert (await _post(http, key, "/query", {"kind": "Part"})).status_code == 403
        # far-side Records of another Kind through Links
        assert await _status(http, key, "/kinds/Supplier/records/S-1005/links/supplies") == 403
        assert await _status(http, key, "/relations/supplied_by/links") == 403
        # Relation predicates and includes reaching another Kind
        via = {"kind": "Supplier", "where": {"via": "supplies"}}
        assert (await _post(http, key, "/query", via)).status_code == 403
        count = {"kind": "Supplier", "where": {"count": "supplies", "op": "gte", "value": 1}}
        assert (await _post(http, key, "/query", count)).status_code == 403
        include = {"kind": "Supplier", "include": {"supplies": {"limit": 1}}}
        assert (await _post(http, key, "/query", include)).status_code == 403
        where = '{"via": "supplies"}'
        assert await _status(http, key, "/kinds/Supplier/records", where=where) == 403
        # traversal hops into another Kind
        hop = {"start": {"kind": "Supplier", "key": "S-1005"}, "path": [{"relation": "supplies"}]}
        assert (await _post(http, key, "/traverse", hop)).status_code == 403
        # history is not limited to Kinds: refused
        assert await _status(http, key, f"/revisions/{revs['data']}") == 403

        # Link counts leave out Relations to other Kinds, also when a wider caller's response
        # for the same request is cached
        record = "/kinds/Supplier/records/S-1005"
        assert await _status(http, domain_key, record, links="true") == 200
        counted = await http.get(
            BASE + record, params={"links": "true"}, headers={"Authorization": f"Bearer {key}"}
        )
        assert counted.status_code == 200
        assert "supplies" not in {c["relation"] for c in counted.json()["record"]["links"]}

        # neighborhoods keep to the readable Kinds
        hood = await _post(
            http, key, "/neighborhood", {"seeds": [{"kind": "Supplier", "key": "S-1005"}]}
        )
        assert hood.status_code == 200
        assert {n["kind"] for n in hood.json()["nodes"]} == {"Supplier"}
        assert hood.json()["edges"] == []
        seed = {"seeds": [{"kind": "Part", "key": "P-10001"}]}
        assert (await _post(http, key, "/neighborhood", seed)).status_code == 403

        # search filters to the readable Kinds, and refuses others named explicitly
        found = await _post(http, key, "/search", {"q": "Kavsen Aloys"})
        assert found.status_code == 200
        assert found.json()["items"]
        assert {i["kind"] for i in found.json()["items"]} == {"Supplier"}
        named = await _post(http, key, "/search", {"q": "bearing", "kinds": ["Part"]})
        assert named.status_code == 403


async def test_kind_grant_keeps_agent_reads_to_its_kind(seeded: Seeded) -> None:
    key = seeded.facts["kind_key"]
    domain_key = seeded.facts["domain_key"]
    async with keeper(seeded.app_url, _auth_section()) as http:
        # context packs: seeds of another Kind are refused, the neighborhood keeps to Supplier,
        # also when a wider caller's pack for the same request is cached
        body = {"refs": ["Supplier/S-1005"], "budget_tokens": 32000}
        wide = await _post(http, domain_key, "/context", body)
        assert wide.status_code == 200
        assert {n["ref"].partition("/")[0] for n in wide.json()["neighbors"]} - {"Supplier"}
        pack = await _post(http, key, "/context", body)
        assert pack.status_code == 200, pack.text
        assert pack.json()["etag"] != wide.json()["etag"]
        assert {n["ref"].partition("/")[0] for n in pack.json()["neighbors"]} <= {"Supplier"}
        assert all(
            ref.partition("/")[0] == "Supplier"
            for links in pack.json()["links"].values()
            for link in links
            for ref in (link["from"], link["to"])
        )
        assert "Part/" not in pack.text
        assert "PurchaseOrder/" not in pack.text
        other = {"refs": ["Supplier/S-1005", "Part/P-10001"]}
        assert (await _post(http, key, "/context", other)).status_code == 403
        query = {"query": {"kind": "Part", "limit": 2}}
        assert (await _post(http, key, "/context", query)).status_code == 403

        # explanations: the path Kind is authorized, Links keep to Supplier
        explained = await http.get(
            BASE + "/kinds/Supplier/records/S-1005/explain",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert explained.status_code == 200, explained.text
        assert all(link["ref"].startswith("Supplier/") for link in explained.json()["links"])
        assert await _status(http, key, "/kinds/Part/records/P-10001/explain") == 403

        # Record history and lineage: the path Kind is authorized like a Record read
        assert await _status(http, key, "/kinds/Supplier/records/S-1005/history") == 200
        assert await _status(http, key, "/kinds/Supplier/records/S-1005/lineage") == 200
        assert await _status(http, key, "/kinds/Part/records/P-10001/history") == 403
        assert await _status(http, key, "/kinds/Part/records/P-10001/lineage") == 403

        # the change feed is history, not limited to Kinds: refused like the branch log
        assert await _status(http, key, "/branches/main/changes") == 403
        assert await _status(http, key, "/branches/main/events") == 403
        assert await _status(http, key, "/branches/main/log") == 403


async def test_source_runs_need_a_domain_wide_grant(seeded: Seeded) -> None:
    runs = "/sources/approved.suppliers/runs"
    async with keeper(seeded.app_url, _auth_section()) as http:
        assert await _status(http, seeded.facts["domain_key"], runs) == 200
        assert await _status(http, seeded.facts["branch_key"], runs) == 403
        assert await _status(http, seeded.facts["kind_key"], runs) == 403
        # Narrowed callers are not told whether a Source exists either.
        assert await _status(http, seeded.facts["kind_key"], "/sources/nowhere/runs") == 403


# Event streams are authorized again while open ---------------------------------------------


@asynccontextmanager
async def _owner_store(owner_url: str) -> AsyncIterator[SqlAuthStore]:
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        yield SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


async def _stream_reader(
    owner_url: str, name: str, networks: tuple[str, ...] = ()
) -> tuple[str, str, str]:
    """A reader of the whole Domain: its API key (usable from `networks` only, when given),
    the key id and the grant id."""
    async with _owner_store(owner_url) as store, store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        grant = await unit.grants.add(
            domain_id=DOMAIN, subject=principal.id, role="viewer", created_by="usr_seed"
        )
        generated = CODEC.generate()
        key = await unit.api_keys.add(
            principal_id=principal.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            allowed_networks=networks,
        )
    return generated.token, key.id, grant.id


async def _timed_stream(
    seeded: Seeded,
    token: str,
    reauthorize_s: float,
    max_s: float,
    during: Callable[[], Awaitable[None]] | None = None,
) -> tuple[httpx.Response, float]:
    """Open the main branch stream, run `during` while it is open, and time the stream."""
    section = _auth_section()
    section["auth"]["authz"] = {"grants_cache_ttl_s": 0.2}
    feed = {
        "poll_interval_s": 0.05,
        "heartbeat_s": 30,
        "reauthorize_interval_s": reauthorize_s,
        "max_connection_s": max_s,
    }
    # Keeper sends the 200 of a stream only after authorizing the caller. The in-process client
    # returns the response when the stream ends, so the application's own send is the signal.
    open_stream = asyncio.Event()

    def stream_started(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start" and message["status"] == 200:
            open_stream.set()

    async with keeper(
        seeded.app_url, section, on_send=stream_started, agents={"feed": feed}
    ) as http:
        started = time.monotonic()
        listening = asyncio.create_task(
            http.get(f"{BASE}/branches/main/events", headers={"Authorization": f"Bearer {token}"})
        )
        await asyncio.wait_for(open_stream.wait(), timeout=10)  # connected and authorized
        assert not listening.done()
        if during is not None:
            await during()
        response = await asyncio.wait_for(listening, timeout=max_s + 5)
    return response, time.monotonic() - started


@pytest.mark.parametrize("withdrawn", ["grant", "key"])
async def test_a_stream_closes_once_its_access_is_withdrawn(seeded: Seeded, withdrawn: str) -> None:
    token, key_id, grant_id = await _stream_reader(seeded.owner_url, f"stream reader ({withdrawn})")

    async def withdraw() -> None:
        async with _owner_store(seeded.owner_url) as store, store.unit() as unit:
            if withdrawn == "grant":
                assert await unit.grants.delete(DOMAIN, grant_id)
            else:
                assert await unit.api_keys.revoke(key_id, datetime.now(UTC))

    response, elapsed = await _timed_stream(seeded, token, 0.3, 8, withdraw)
    assert response.status_code == 200
    assert response.text.startswith("retry: ")
    # within the interval plus the grants cache, far before max_connection_s
    assert elapsed < 3, elapsed
    async with keeper(seeded.app_url, _auth_section()) as http:
        assert await _status(http, token, "/branches/main/events") in (401, 403)


async def test_an_authorized_stream_stays_open(seeded: Seeded) -> None:
    token, _, _ = await _stream_reader(seeded.owner_url, "steady stream reader")
    response, elapsed = await _timed_stream(seeded, token, 0.2, 1.5)
    assert response.status_code == 200
    # several checks passed: the stream ran until max_connection_s
    assert elapsed >= 1.4, elapsed


async def test_a_stream_of_a_key_bound_to_networks_stays_open(seeded: Seeded) -> None:
    # The in-process client connects from 127.0.0.1: each check must see that address.
    token, _, _ = await _stream_reader(seeded.owner_url, "local stream reader", ("127.0.0.1/32",))
    response, elapsed = await _timed_stream(seeded, token, 0.2, 1.5)
    assert response.status_code == 200
    assert elapsed >= 1.4, elapsed
