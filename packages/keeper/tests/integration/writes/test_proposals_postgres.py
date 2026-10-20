"""Proposals through the API against Postgres: reviews, merge, Conflicts, resolutions."""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, Record, Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.revision import Author, Origin
from lorebase_core.types import encode_value
from lorebase_core.validate import RecordUpsert
from lorebase_store.commit import SchemaUpsert, commit
from lorebase_store.db import create_engine
from lorebase_store.materialize import verify
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

pytestmark = pytest.mark.integration

API = "/api/v1"
DOMAIN = f"{API}/domains/supply"
PROPOSALS = f"{DOMAIN}/proposals"
EXAMPLE = Path(__file__).resolve().parents[5] / "examples" / "supply"
LOADER = Author(principal="usr_loader")


def supply_schema() -> Schema:
    """The example schema without the elements that need expressions (not enabled yet)."""
    document: dict[str, Any] = yaml.safe_load((EXAMPLE / "schema.yaml").read_text())
    del document["operations"]
    del document["derivations"]["single_sourced"]
    del document["kinds"]["Part"]["attributes"]["single_sourced"]
    return load_schema(yaml.safe_dump(document, sort_keys=False))


SCHEMA = supply_schema()
SUPPLIER: Kind = next(k for k in SCHEMA.kinds.values() if k.api_name == "Supplier")
ATTRIBUTE = {a.api_name: a for a in SUPPLIER.attributes.values()}
CITY = ATTRIBUTE["city"].id


def supplier_rows(limit: int = 3) -> dict[str, dict[str, str]]:
    with (EXAMPLE / "data" / "supplier.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))[:limit]
    return {row["supplier_code"]: row for row in rows}


def supplier(row: dict[str, str], **changes: str) -> RecordUpsert:
    merged = {**row, **changes}
    values = {
        ATTRIBUTE[name].id: encode_value(ATTRIBUTE[name].type, value)
        for name, value in merged.items()
        if value != ""
    }
    return RecordUpsert(record=Record(kind=SUPPLIER.id, key=merged["supplier_code"], values=values))


class Loader:
    """Writes through the store commit pipeline directly, as the superuser."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.runtime = StoreRuntime.from_settings(StoreSettings())
        self.rows = supplier_rows()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            yield session

    async def load(self) -> None:
        async with self.session() as session:
            await commit(
                session,
                "supply",
                "main",
                [supplier(row) for row in self.rows.values()],
                author=LOADER,
                message="Load suppliers",
                origin=Origin(type="import"),
                schema_changes=[SchemaUpsert(element) for element in SCHEMA.elements()],
                runtime=self.runtime,
            )

    async def edit(self, branch: str, code: str, **changes: str) -> bytes:
        async with self.session() as session:
            result = await commit(
                session,
                "supply",
                branch,
                [supplier(self.rows[code], **changes)],
                author=LOADER,
                message=f"Edit {code} on {branch}",
                origin=Origin(type="import"),
                runtime=self.runtime,
            )
        return result.revision_id

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.engine.connect() as connection:
            await connection.execute(text("SET lorebase.bypass_rls = 'on'"))
            return (await connection.execute(text(sql), params)).scalar_one()

    async def verify(self, branch: str) -> None:
        async with self.session() as session, session.begin():
            report = await verify(session, "supply", branch, runtime=self.runtime)
        assert report.ok, report


@pytest.fixture
async def loader(database_url: str) -> AsyncIterator[Loader]:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        yield Loader(engine)
    finally:
        await engine.dispose()


async def setup_domain(http: Any, admin: dict[str, str], loader: Loader, *branches: str) -> None:
    created = await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
    assert created.status_code == 201, created.text
    await loader.load()
    for name in branches:
        response = await http.post(f"{DOMAIN}/branches", json={"name": name}, headers=admin)
        assert response.status_code == 201, response.text


def proposal_body(source: str, target: str = "main") -> dict[str, str]:
    return {"source_ref": source, "target_ref": target, "title": f"{source} into {target}"}


async def test_merge_with_conflicts_end_to_end(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        await setup_domain(http, admin, loader, "left", "right")
        editor = await role_key(http, "supply", "editor")
        reviewer = await role_key(http, "supply", "reviewer")
        maintainer = await role_key(http, "supply", "maintainer")
        viewer = await role_key(http, "supply", "viewer")
        await loader.edit("left", "S-1001", city="Gdansk")
        await loader.edit("right", "S-1001", city="Warsaw")
        await loader.edit("right", "S-1002", city="Lodz")

        # left fast-forwards main once approved
        denied = await http.post(PROPOSALS, json=proposal_body("left"), headers=viewer)
        assert denied.status_code == 403
        assert denied.json()["permission"] == "proposal.create"
        first = await http.post(
            PROPOSALS, json=proposal_body("left"), headers={**editor, "Idempotency-Key": "p-1"}
        )
        assert first.status_code == 201, first.text
        left = first.json()
        assert left["attempt"]["open_conflicts"] == 0
        assert left["approval"] == {
            "protected": True,
            "required": 1,
            "approvals": 0,
            "approvers": [],
            "changes_requested": [],
            "satisfied": False,
        }
        again = await http.post(
            PROPOSALS, json=proposal_body("left"), headers={**editor, "Idempotency-Key": "p-1"}
        )
        assert again.status_code == 200
        assert again.headers["Idempotent-Replayed"] == "true"
        assert again.json()["id"] == left["id"]
        duplicate = await http.post(PROPOSALS, json=proposal_body("left"), headers=editor)
        assert duplicate.status_code == 409

        merge_left = f"{PROPOSALS}/{left['id']}/merge"
        assert (await http.post(merge_left, headers=editor)).status_code == 403
        unapproved = await http.post(merge_left, headers=maintainer)
        assert unapproved.status_code == 409
        assert unapproved.json()["code"] == "approvals-required"
        approved = await http.post(
            f"{PROPOSALS}/{left['id']}/reviews", json={"verdict": "approve"}, headers=reviewer
        )
        assert approved.status_code == 201, approved.text
        assert approved.json()["approval"]["satisfied"] is True
        forwarded = await http.post(merge_left, headers={**maintainer, "Idempotency-Key": "m-1"})
        assert forwarded.status_code == 200, forwarded.text
        assert forwarded.json()["result"] == "fast_forward"
        left_head = (await http.get(f"{DOMAIN}/branches/left", headers=admin)).json()["head"]
        main = (await http.get(f"{DOMAIN}/branches/main", headers=admin)).json()
        assert main["head"] == forwarded.json()["revision"] == left_head
        replayed = await http.post(merge_left, headers={**maintainer, "Idempotency-Key": "m-1"})
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True
        assert (await http.post(merge_left, headers=maintainer)).json()["code"] == (
            "proposal-not-open"
        )

        # right now conflicts with main on the city of S-1001
        created = await http.post(PROPOSALS, json=proposal_body("right"), headers=editor)
        assert created.status_code == 201, created.text
        right = created.json()
        assert right["attempt"]["open_conflicts"] == 1
        assert right["checks"][0] == {
            "name": "merge",
            "status": "failed",
            "message": right["checks"][0]["message"],
        }
        base = f"{PROPOSALS}/{right['id']}"
        self_approval = await http.post(
            f"{base}/reviews", json={"verdict": "approve"}, headers=editor
        )
        assert self_approval.status_code == 403  # editor lacks proposal.review
        changes = await http.post(
            f"{base}/reviews",
            json={"verdict": "request_changes", "comment": "check the city"},
            headers=reviewer,
        )
        assert changes.json()["approval"]["changes_requested"]
        blocked = await http.post(f"{base}/merge", headers=maintainer)
        assert blocked.json()["code"] == "approvals-required"
        await http.post(f"{base}/reviews", json={"verdict": "approve"}, headers=reviewer)

        conflicted = await http.post(f"{base}/merge", headers=maintainer)
        assert conflicted.status_code == 409, conflicted.text
        problem = conflicted.json()
        assert problem["code"] == "merge-conflicts"
        attempt = problem["attempt"]
        assert attempt["open_conflicts"] == 1
        [conflict] = attempt["conflicts"]
        assert conflict["kind"] == "DV1"
        assert conflict["key"] == "S-1001"
        assert conflict["attribute"] == CITY
        assert (conflict["ours"], conflict["theirs"]) == ("Gdansk", "Warsaw")
        assert conflict["status"] == "open"

        listing = await http.get(
            f"{base}/conflicts", params={"kind": "DV1", "status": "open"}, headers=viewer
        )
        assert listing.status_code == 200, listing.text
        assert [c["id"] for c in listing.json()["conflicts"]] == [conflict["id"]]
        assert listing.json()["recomputed"] is False
        none = await http.get(f"{base}/conflicts", params={"kind": "XS8"}, headers=viewer)
        assert none.json()["conflicts"] == []
        bad = await http.get(f"{base}/conflicts", params={"kind": "ZZ9"}, headers=viewer)
        assert bad.status_code == 422

        resolution = f"{base}/conflicts/{conflict['id']}/resolution"
        assert (
            await http.post(resolution, json={"choice": "theirs"}, headers=viewer)
        ).status_code == 403
        wrong = await http.post(resolution, json={"choice": "drop"}, headers=reviewer)
        assert wrong.status_code == 422
        unknown = await http.post(
            f"{base}/conflicts/{'0' * 64}/resolution", json={"choice": "ours"}, headers=reviewer
        )
        assert unknown.status_code == 404
        stored = await http.post(resolution, json={"choice": "theirs"}, headers=reviewer)
        assert stored.status_code == 200, stored.text
        assert stored.json()["choice"] == "theirs"

        merged = await http.post(
            f"{base}/merge", json={"message": "Merge right"}, headers=maintainer
        )
        assert merged.status_code == 200, merged.text
        outcome = merged.json()
        assert outcome["result"] == "merged"
        assert outcome["proposal"]["status"] == "merged"
        assert outcome["attempt"]["resolved_conflicts"] == 1
        revision = outcome["revision"]
        main = (await http.get(f"{DOMAIN}/branches/main", headers=admin)).json()
        assert main["head"] == revision
        detail = (await http.get(base, headers=viewer)).json()
        assert detail["merged_revision"] == revision
        closed = await http.post(f"{base}/close", headers=editor)
        assert closed.json()["code"] == "proposal-not-open"

    parents = await loader.scalar(
        "SELECT count(*) FROM revision_parents WHERE domain_id = 'supply' "
        "AND revision_id = decode(:revision, 'hex')",
        revision=revision,
    )
    assert parents == 2
    city = await loader.scalar(
        "SELECT values ->> :city FROM mat_records WHERE domain_id = 'supply' "
        "AND branch = 'main' AND key = 'S-1001'",
        city=CITY,
    )
    assert city == "Warsaw"
    other = await loader.scalar(
        "SELECT values ->> :city FROM mat_records WHERE domain_id = 'supply' "
        "AND branch = 'main' AND key = 'S-1002'",
        city=CITY,
    )
    assert other == "Lodz"
    origin = await loader.scalar(
        "SELECT p.origin_type FROM mat_records r JOIN provenance p "
        "ON p.domain_id = r.domain_id AND p.id = r.lineage ->> :city "
        "WHERE r.domain_id = 'supply' AND r.branch = 'main' AND r.key = 'S-1001'",
        city=CITY,
    )
    assert origin == "merge_resolution"
    audited = await loader.scalar(
        "SELECT count(DISTINCT action) FROM audit_log WHERE action IN "
        "('proposal.create', 'proposal.review', 'proposal.merge', 'proposal.resolve')"
    )
    assert audited == 4
    await loader.verify("main")


async def test_stale_attempt_is_recomputed(
    keeper: Any, installation_admin: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper(proposals={"required_approvals": 0, "attempt_retention": 2}) as http:
        await setup_domain(http, admin, loader, "work")
        await loader.edit("work", "S-1001", city="Warsaw")
        await loader.edit("main", "S-1001", city="Gdansk")
        created = await http.post(PROPOSALS, json=proposal_body("work"), headers=admin)
        assert created.status_code == 201, created.text
        base = f"{PROPOSALS}/{created.json()['id']}"
        [conflict] = (await http.get(f"{base}/conflicts", headers=admin)).json()["conflicts"]
        stored = await http.post(
            f"{base}/conflicts/{conflict['id']}/resolution", json={"choice": "ours"}, headers=admin
        )
        assert stored.status_code == 200, stored.text

        # the target moves elsewhere: the attempt is recomputed and the resolution kept
        await loader.edit("main", "S-1003", city="Torun")
        moved = (await http.get(f"{base}/conflicts", headers=admin)).json()
        assert moved["recomputed"] is True
        assert moved["stale"] is True
        assert moved["open_conflicts"] == 0
        assert moved["resolved_conflicts"] == 1
        assert moved["conflicts"][0]["id"] == conflict["id"]
        assert moved["unmatched_resolutions"] == []

        # the target changes the conflicting value again: a new Conflict, the old resolution
        # no longer matches
        await loader.edit("main", "S-1001", city="Krakow")
        changed = (await http.get(f"{base}/conflicts", headers=admin)).json()
        assert changed["stale"] is True
        assert changed["open_conflicts"] == 1
        assert changed["unmatched_resolutions"] == [conflict["id"]]
        assert [r["matched"] for r in changed["resolutions"]] == [False]
        [fresh] = changed["conflicts"]
        assert fresh["id"] != conflict["id"]
        assert fresh["ours"] == "Krakow"

        bulk = await http.post(
            f"{base}/conflicts:resolve",
            json={
                "resolutions": [{"fingerprint": fresh["id"], "choice": "custom", "value": "Lodz"}]
            },
            headers=admin,
        )
        assert bulk.status_code == 200, bulk.text
        assert bulk.json()["open_conflicts"] == 0
        assert bulk.json()["status"] == "resolved"
        merged = await http.post(f"{base}/merge", headers=admin)
        assert merged.status_code == 200, merged.text
    city = await loader.scalar(
        "SELECT values ->> :city FROM mat_records WHERE domain_id = 'supply' "
        "AND branch = 'main' AND key = 'S-1001'",
        city=CITY,
    )
    assert city == "Lodz"
    attempts = await loader.scalar("SELECT count(*) FROM merge_attempts WHERE domain_id = 'supply'")
    assert attempts == 2
    await loader.verify("main")


async def test_auto_close_and_unprotected_target(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper(proposals={"auto_close_source_branch": True}) as http:
        await setup_domain(http, admin, loader, "feature", "staging")
        maintainer = await role_key(http, "supply", "maintainer")
        await loader.edit("feature", "S-1002", city="Lodz")
        # staging is not protected: no approval needed
        created = await http.post(
            PROPOSALS, json=proposal_body("feature", "staging"), headers=maintainer
        )
        assert created.status_code == 201, created.text
        assert created.json()["approval"]["protected"] is False
        merged = await http.post(f"{PROPOSALS}/{created.json()['id']}/merge", headers=maintainer)
        assert merged.status_code == 200, merged.text
        assert merged.json()["result"] == "fast_forward"
        assert merged.json()["source_deleted"] is True
        gone = await http.get(f"{DOMAIN}/branches/feature", headers=admin)
        assert gone.status_code == 404
        listing = await http.get(PROPOSALS, params={"status": "merged"}, headers=admin)
        assert [p["id"] for p in listing.json()["items"]] == [created.json()["id"]]
    await loader.verify("staging")
