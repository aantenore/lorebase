"""Agent writes through the API against Postgres: task branches, Proposals and merges.

An agent (a service principal with an API key) executes Operations on the protected `main` of
the `supply` example; Keeper runs them on `agent/{principal}/{task}` and opens the Proposal.
"""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, Record
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
FLAG = f"{DOMAIN}/operations/flag_supplier_risk/execute"
UPDATE = f"{DOMAIN}/operations/update_supplier/execute"
RISK = {"supplier": "S-1001", "severity": "high", "reason": "Two late deliveries in a row"}


def slug(principal_id: str) -> str:
    return principal_id.lower()


# Data ------------------------------------------------------------------------------------


@dataclass
class Loader:
    """Loads the example schema and Suppliers into `main` as the database owner."""

    engine: AsyncEngine
    agent_id: str

    def __post_init__(self) -> None:
        document = yaml.safe_load((EXAMPLE / "schema.yaml").read_text())
        document["operations"]["flag_supplier_risk"]["permissions"].append(self.agent_id)
        self.schema = load_schema(yaml.safe_dump(document, sort_keys=False))
        self.supplier: Kind = next(
            k for k in self.schema.kinds.values() if k.api_name == "Supplier"
        )
        self.attribute = {a.api_name: a for a in self.supplier.attributes.values()}
        with (EXAMPLE / "data" / "supplier.csv").open(newline="") as handle:
            self.rows = {r["supplier_code"]: r for r in list(csv.DictReader(handle))[:3]}
        self.runtime = StoreRuntime.from_settings(StoreSettings())

    def upsert(self, code: str, **changes: str) -> RecordUpsert:
        merged = {**self.rows[code], **changes}
        values = {
            self.attribute[name].id: encode_value(self.attribute[name].type, value)
            for name, value in merged.items()
            if value != ""
        }
        record = Record(kind=self.supplier.id, key=code, values=values)
        return RecordUpsert(record=record)

    async def _commit(self, branch: str, **options: Any) -> bytes:
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            result = await commit(
                session,
                "supply",
                branch,
                author=LOADER,
                origin=Origin(type="import"),
                runtime=self.runtime,
                **options,
            )
        return result.revision_id

    async def load(self) -> None:
        await self._commit(
            "main",
            changes=[self.upsert(code) for code in self.rows],
            message="Load suppliers",
            schema_changes=[SchemaUpsert(element) for element in self.schema.elements()],
        )

    async def edit(self, branch: str, code: str, **changes: str) -> bytes:
        return await self._commit(
            branch, changes=[self.upsert(code, **changes)], message=f"Edit {code} on {branch}"
        )

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.engine.connect() as connection:
            await connection.execute(text("SET lorebase.bypass_rls = 'on'"))
            return (await connection.execute(text(sql), params)).scalar_one()

    async def verify(self, branch: str) -> None:
        async with async_sessionmaker(self.engine)() as session, session.begin():
            await _verified(session, branch, self.runtime)


async def _verified(session: AsyncSession, branch: str, runtime: StoreRuntime) -> None:
    report = await verify(session, "supply", branch, runtime=runtime)
    assert report.ok, report


@dataclass
class World:
    http: Any
    admin: dict[str, str]
    agent: dict[str, str]
    agent_id: str
    reviewer: dict[str, str]
    maintainer: dict[str, str]
    loader: Loader

    @property
    def owner(self) -> str:
        return f"agent/{slug(self.agent_id)}"

    async def branch(self, name: str) -> Any:
        return await self.http.get(f"{DOMAIN}/branches/{name}", headers=self.admin)

    async def head(self, name: str = "main") -> str:
        found = await self.branch(name)
        assert found.status_code == 200, found.text
        return str(found.json()["head"])

    async def flags(self, branch: str) -> list[dict[str, Any]]:
        listed = await self.http.get(
            f"{DOMAIN}/kinds/RiskFlag/records", params={"branch": branch}, headers=self.admin
        )
        assert listed.status_code == 200, listed.text
        return list(listed.json()["items"])

    async def write(self, url: str, parameters: dict[str, Any], **body: Any) -> Any:
        return await self.http.post(
            url, json={"parameters": parameters, **body}, headers=self.agent
        )

    async def approve(self, proposal: str) -> None:
        reviewed = await self.http.post(
            f"{PROPOSALS}/{proposal}/reviews", json={"verdict": "approve"}, headers=self.reviewer
        )
        assert reviewed.status_code == 201, reviewed.text


@pytest.fixture
def start(keeper: Any, installation_admin: Any, role_key: Any, database_url: str) -> Any:
    """`async with start(domain_settings, **keeper_sections) as world`."""

    @asynccontextmanager
    async def running(
        domain_settings: dict[str, Any] | None = None, **sections: Any
    ) -> AsyncIterator[World]:
        admin = installation_admin.headers
        engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
        try:
            async with keeper(**sections) as http:
                created = await http.post(
                    f"{API}/domains",
                    json={"id": "supply", "settings": domain_settings or {}},
                    headers=admin,
                )
                assert created.status_code == 201, created.text
                key = await http.post(
                    f"{DOMAIN}/api-keys", json={"name": "agent", "role": "editor"}, headers=admin
                )
                assert key.status_code == 201, key.text
                agent_id = key.json()["principal_id"]
                granted = await http.post(
                    f"{DOMAIN}/grants",
                    json={"subject": agent_id, "role": "operator"},
                    headers=admin,
                )
                assert granted.status_code == 201, granted.text
                loader = Loader(engine, agent_id)
                await loader.load()
                yield World(
                    http=http,
                    admin=admin,
                    agent={"Authorization": f"Bearer {key.json()['key']}"},
                    agent_id=agent_id,
                    reviewer=await role_key(http, "supply", "reviewer"),
                    maintainer=await role_key(http, "supply", "maintainer"),
                    loader=loader,
                )
        finally:
            await engine.dispose()

    return running


# Proposed, approved and merged by humans -------------------------------------------------


async def test_agent_write_on_protected_main_is_proposed_then_merged_by_a_human(start: Any) -> None:
    async with start() as w:
        main = await w.head()
        sent = {
            "task": "Audit Q4",
            "intent": "Flag S-1001 after the Q4 audit",
            "idempotency_key": "k1",
        }
        proposed = await w.write(FLAG, RISK, **sent)
        assert proposed.status_code == 202, proposed.text
        body = proposed.json()
        branch = f"{w.owner}/audit-q4"
        assert (body["status"], body["branch"], body["target"]) == ("proposed", branch, "main")
        assert body["proposal"]["status"] == "open"
        assert body["proposal"]["title"] == f"Agent {w.agent_id}: audit-q4"
        assert body["proposal"]["created"] is True
        assert body["branch_created"] is True
        assert body["approval"] == {"required": 1, "approvals": 0, "satisfied": False}
        assert body["attempt"]["open_conflicts"] == 0
        assert body["merge"] == {
            "requested": False,
            "allowed": False,
            "performed": False,
            "blockers": ["approvals-pending"],
        }
        assert body["revision"] == body["revisions"][-1]
        assert [s["action"] for s in body["next"]] == ["await_review", "get_proposal"]
        assert await w.head() == main  # nothing reached main
        assert await w.head(branch) == body["revision"]
        assert len(await w.flags(branch)) == 1
        assert await w.flags("main") == []

        detail = (await w.http.get(f"{PROPOSALS}/{body['proposal']['id']}", headers=w.admin)).json()
        assert detail["origin"] == "operation"
        assert detail["author"] == w.agent_id
        assert detail["description"].startswith("Flag S-1001 after the Q4 audit\n\nOperations:\n")
        assert 'flag_supplier_risk(reason="Two late deliveries in a row"' in detail["description"]
        assert body["invocation_id"] in detail["description"]

        # the same request again changes nothing: one commit, one Proposal
        again = await w.write(FLAG, RISK, **sent)
        assert again.status_code == 202, again.text
        assert again.headers["Idempotent-Replayed"] == "true"
        assert again.json()["proposal"]["id"] == body["proposal"]["id"]
        assert again.json()["proposal"]["created"] is False
        assert again.json()["invocation_id"] == body["invocation_id"]
        assert await w.head(branch) == body["revision"]

        # a second Operation on the same task extends the Proposal
        second = await w.write(
            FLAG,
            {**RISK, "supplier": "S-1002", "severity": "low"},
            task="audit-q4",
            idempotency_key="k2",
        )
        assert second.status_code == 202, second.text
        assert second.json()["proposal"]["id"] == body["proposal"]["id"]
        assert second.json()["proposal"]["created"] is False
        detail = (await w.http.get(f"{PROPOSALS}/{body['proposal']['id']}", headers=w.admin)).json()
        assert detail["description"].count("\n- flag_supplier_risk(") == 2
        assert detail["description"].startswith("Flag S-1001 after the Q4 audit")
        assert len(await w.flags(branch)) == 2

        # an agent cannot merge its own work; a maintainer can once a reviewer approved
        proposal = body["proposal"]["id"]
        merge = f"{PROPOSALS}/{proposal}/merge"
        assert (await w.http.post(merge, headers=w.agent)).status_code == 403
        assert (await w.http.post(merge, headers=w.maintainer)).json()["code"] == (
            "approvals-required"
        )
        await w.approve(proposal)
        merged = await w.http.post(merge, headers=w.maintainer)
        assert merged.status_code == 200, merged.text
        assert merged.json()["source_deleted"] is True
        assert (await w.branch(branch)).status_code == 404
        assert await w.head() == merged.json()["revision"]
        assert len(await w.flags("main")) == 2
        await w.loader.verify("main")
        for action, expected in (
            ("operation.propose", 3),
            ("operation.invoke", 2),
            ("proposal.create", 1),
            ("proposal.update", 1),
            ("branch.create", 1),
        ):
            count = await w.loader.scalar(
                "SELECT count(*) FROM audit_log WHERE action = :a", a=action
            )
            assert count == expected, action


async def test_closing_a_proposal_deletes_the_task_branch_unless_another_one_uses_it(
    start: Any,
) -> None:
    async with start() as w:
        first = await w.write(FLAG, RISK, task="one")
        assert first.status_code == 202, first.text
        branch = first.json()["branch"]
        closed = await w.http.post(
            f"{PROPOSALS}/{first.json()['proposal']['id']}/close", headers=w.agent
        )
        assert closed.status_code == 200, closed.text
        assert (await w.branch(branch)).status_code == 404

        # a second open Proposal from the same branch keeps it
        second = await w.write(FLAG, RISK, task="two")
        branch = second.json()["branch"]
        await w.http.post(f"{DOMAIN}/branches", json={"name": "elsewhere"}, headers=w.admin)
        other = await w.http.post(
            PROPOSALS,
            json={"source_ref": branch, "target_ref": "elsewhere", "title": "Also here"},
            headers=w.admin,
        )
        assert other.status_code == 201, other.text
        closed = await w.http.post(
            f"{PROPOSALS}/{second.json()['proposal']['id']}/close", headers=w.agent
        )
        assert closed.status_code == 200
        assert (await w.branch(branch)).status_code == 200


async def test_the_branch_is_kept_when_deleting_is_turned_off(start: Any) -> None:
    async with start(agent_writes={"delete_branch_on_merge": False}) as w:
        proposed = await w.write(FLAG, RISK, task="keep")
        proposal = proposed.json()["proposal"]["id"]
        await w.approve(proposal)
        merged = await w.http.post(f"{PROPOSALS}/{proposal}/merge", headers=w.maintainer)
        assert merged.json()["source_deleted"] is False
        assert (await w.branch(proposed.json()["branch"])).status_code == 200


# Automatic merges ------------------------------------------------------------------------


async def test_an_allowed_role_merges_automatically_when_nothing_is_pending(start: Any) -> None:
    async with start(
        proposals={"required_approvals": 0},
        agent_writes={"allowed_roles_for_auto_merge": ["editor"]},
    ) as w:
        sent = {"task": "audit", "auto_merge": True, "idempotency_key": "auto-1"}
        merged = await w.write(FLAG, RISK, **sent)
        assert merged.status_code == 202, merged.text
        body = merged.json()
        assert body["status"] == "merged"
        assert body["merge"] == {
            "requested": True,
            "allowed": True,
            "performed": True,
            "blockers": [],
        }
        assert body["proposal"]["status"] == "merged"
        assert body["merged_revision"] == await w.head()
        assert body["branch_deleted"] is True
        assert [s["action"] for s in body["next"]] == ["refresh"]
        assert (await w.branch(body["branch"])).status_code == 404
        assert len(await w.flags("main")) == 1

        # the answer got lost: the retry finds the merged Proposal although the branch is gone
        retried = await w.write(FLAG, RISK, **sent)
        assert retried.status_code == 202, retried.text
        assert retried.json()["status"] == "merged"
        assert retried.json()["replayed"] is True
        assert retried.json()["proposal"]["id"] == body["proposal"]["id"]
        assert len(await w.flags("main")) == 1
        await w.loader.verify("main")


async def test_the_role_must_be_allowed_by_the_domain(start: Any) -> None:
    allowed_roles = {"agent_writes": {"allowed_roles_for_auto_merge": ["maintainer"]}}
    async with start(allowed_roles, proposals={"required_approvals": 0}) as w:
        refused = await w.write(FLAG, RISK, task="audit", auto_merge=True)
        assert refused.status_code == 202, refused.text
        body = refused.json()
        assert body["status"] == "proposed"
        assert body["merge"] == {
            "requested": True,
            "allowed": False,
            "performed": False,
            "blockers": [],
        }
        assert await w.flags("main") == []


async def test_a_domain_can_make_merging_the_default_for_a_role(start: Any) -> None:
    domain = {"agent_writes": {"allowed_roles_for_auto_merge": ["editor"], "auto_merge": True}}
    async with start(domain, proposals={"required_approvals": 0}) as w:
        merged = await w.write(FLAG, RISK, task="audit")
        assert merged.status_code == 202, merged.text
        assert merged.json()["status"] == "merged"
        assert merged.json()["merge"]["requested"] is True


async def test_required_approvals_are_never_skipped(start: Any) -> None:
    async with start(agent_writes={"allowed_roles_for_auto_merge": ["editor"]}) as w:
        held = await w.write(FLAG, RISK, task="audit", auto_merge=True)
        body = held.json()
        assert held.status_code == 202, held.text
        assert body["status"] == "proposed"
        assert body["merge"]["blockers"] == ["approvals-pending"]
        assert body["next"][0]["action"] == "await_review"
        assert await w.flags("main") == []


async def test_a_review_policy_needs_an_actual_approval(start: Any) -> None:
    async with start(
        proposals={"required_approvals": 0},
        agent_writes={"allowed_roles_for_auto_merge": ["editor"]},
    ) as w:
        critical = {**RISK, "severity": "critical"}
        created = await w.http.post(f"{DOMAIN}/branches", json={"name": "work"}, headers=w.admin)
        assert created.status_code == 201, created.text
        # critical flags need review on any branch (review policy), even an unprotected one
        refused = await w.write(FLAG, critical, branch="work")
        assert refused.status_code == 409
        assert (refused.json()["code"], refused.json()["reason"]) == (
            "review-required",
            "review-policy",
        )
        held = await w.write(FLAG, critical, branch="work", task="critical", auto_merge=True)
        assert held.status_code == 202, held.text
        assert held.json()["status"] == "proposed"
        assert held.json()["merge"]["blockers"] == ["review-policy-needs-approval"]
        assert await w.flags("work") == []

        await w.approve(held.json()["proposal"]["id"])
        merged = await w.write(
            FLAG,
            {**critical, "reason": "Fire at the plant"},
            branch="work",
            task="critical",
            auto_merge=True,
        )
        assert merged.json()["status"] == "merged", merged.text
        assert len(await w.flags("work")) == 2


async def test_a_concurrent_human_edit_turns_into_conflicts(start: Any) -> None:
    async with start(
        proposals={"required_approvals": 0},
        agent_writes={"allowed_roles_for_auto_merge": ["editor"]},
    ) as w:
        move = {"key": "S-1001", "city": "Gdansk"}
        first = await w.write(UPDATE, move, task="move", intent="Move S-1001 to Gdansk")
        assert first.status_code == 202, first.text
        assert first.json()["status"] == "proposed"

        await w.loader.edit("main", "S-1001", city="Warsaw")
        second = await w.write(
            UPDATE, {"key": "S-1002", "city": "Lodz"}, task="move", auto_merge=True
        )
        assert second.status_code == 202, second.text
        body = second.json()
        assert body["status"] == "conflicts"
        assert body["proposal"]["id"] == first.json()["proposal"]["id"]
        assert body["proposal"]["status"] == "open"
        assert body["attempt"]["open_conflicts"] == 1
        assert body["attempt"]["counts"] == {"DV1": {"open": 1, "resolved": 0}}
        assert body["merge"]["performed"] is False
        assert [s["action"] for s in body["next"]] == [
            "inspect_conflicts",
            "resolve_conflicts",
            "close_and_retry",
        ]
        listed = await w.http.get(body["next"][0]["href"], headers=w.agent)
        assert listed.status_code == 200, listed.text
        assert listed.json()["open_conflicts"] == 1
        city = await w.loader.scalar(
            "SELECT values ->> :city FROM mat_records WHERE domain_id = 'supply' "
            "AND branch = 'main' AND key = 'S-1002'",
            city=w.loader.attribute["city"].id,
        )
        assert city != "Lodz"


# Refusals --------------------------------------------------------------------------------


async def test_without_a_task_the_refusal_stays_and_explains_what_to_do(start: Any) -> None:
    async with start() as w:
        main = await w.head()
        plain = await w.write(FLAG, RISK)
        assert plain.status_code == 409
        problem = plain.json()
        assert (problem["code"], problem["reason"]) == ("review-required", "protected-branch")
        assert problem["proposals"] == PROPOSALS
        assert problem["next"][0]["action"] == "name_a_task"
        assert problem["next"][0]["href"] == FLAG

        no_task = await w.write(FLAG, RISK, auto_propose=True)
        assert no_task.status_code == 422
        assert no_task.json()["errors"][0]["pointer"] == "/body/task"

        declined = await w.write(FLAG, RISK, task="audit", auto_propose=False)
        assert declined.status_code == 409
        assert declined.json()["next"][0]["action"] == "auto_propose"
        assert await w.head() == main
        assert (await w.branch(f"{w.owner}/audit")).status_code == 404


async def test_the_flow_can_be_turned_off_for_a_domain(start: Any) -> None:
    async with start({"agent_writes": {"enabled": False}}) as w:
        refused = await w.write(FLAG, RISK, task="audit")
        assert refused.status_code == 409
        assert refused.json()["next"][0]["action"] == "agent_writes_disabled"


async def test_nothing_is_created_for_edits_that_would_be_refused(start: Any) -> None:
    async with start() as w:
        invalid = await w.write(UPDATE, {"key": "S-1001", "country": "Germany"}, task="bad")
        assert invalid.status_code == 422, invalid.text
        assert invalid.json()["issues"][0]["code"] == "value.pattern"
        assert (await w.branch(f"{w.owner}/bad")).status_code == 404

        stale = await w.write(FLAG, RISK, task="old", expected_head="0" * 64)
        assert stale.status_code == 409
        assert stale.json()["code"] == "stale-head"
        assert (await w.branch(f"{w.owner}/old")).status_code == 404

        dry = await w.write(FLAG, RISK, task="dry", dry_run=True)
        assert dry.status_code == 200
        assert (dry.json()["status"], dry.json()["review_required"]) == ("dry_run", True)
        assert (await w.branch(f"{w.owner}/dry")).status_code == 404

        await w.loader.edit("main", "S-1003", status="terminated")
        terminated = await w.write(FLAG, {**RISK, "supplier": "S-1003"}, task="gone")
        assert terminated.status_code == 422, terminated.text
        assert terminated.json()["code"] == "precondition-failed"
        assert (await w.branch(f"{w.owner}/gone")).status_code == 404


async def test_the_number_of_task_branches_and_their_owners_are_checked(start: Any) -> None:
    async with start(agent_writes={"max_open_branches_per_principal": 1}) as w:
        taken = await w.http.post(
            f"{DOMAIN}/branches", json={"name": f"{w.owner}/mine"}, headers=w.admin
        )
        assert taken.status_code == 201, taken.text
        other = await w.write(FLAG, RISK, task="mine")
        assert other.status_code == 409
        assert other.json()["code"] == "already-exists"  # not created by this principal

        first = await w.write(FLAG, RISK, task="one")
        assert first.status_code == 409  # the admin's branch under the prefix counts
        assert first.json()["code"] == "agent-branch-limit"
        assert first.json()["branches"] == [f"{w.owner}/mine"]


async def test_the_task_branch_limit_allows_reusing_a_branch(start: Any) -> None:
    async with start(agent_writes={"max_open_branches_per_principal": 1}) as w:
        first = await w.write(FLAG, RISK, task="one")
        assert first.status_code == 202, first.text
        again = await w.write(FLAG, {**RISK, "supplier": "S-1002"}, task="one")
        assert again.status_code == 202, again.text
        assert again.json()["proposal"]["id"] == first.json()["proposal"]["id"]
        blocked = await w.write(FLAG, RISK, task="two")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "agent-branch-limit"
