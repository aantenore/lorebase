"""Reads are authorized against the branch or Revision actually read.

A grant narrowed to `feature/*` must not read `main` data by naming a `main`-only Revision
through `at`, a tag, a Revision route or a diff side.
"""

from __future__ import annotations

from collections.abc import Iterator
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
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.revisions = {
        "schema": schema_commit.revision_id.hex(),
        "data": data.revision_id.hex(),
        "main_only": main_only.revision_id.hex(),
        "feature": feature.revision_id.hex(),
    }
    seeded.facts = {"branch_key": branch_key, "domain_key": domain_key}
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
        assert await _status(http, key, f"/revisions/{revs['data']}/counts") == 403
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
