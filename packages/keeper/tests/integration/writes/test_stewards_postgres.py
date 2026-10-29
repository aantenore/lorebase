"""Stewards through the API against Postgres (design 10.7, AC-37): steward roles from the
schema, assignees computed from grants (a principal who cannot read a Kind the work touches is
none), the `assigned` filters of Proposals and reconciliation candidates, and the protection
rules `required_approvals_from_stewards` and `required_roles`; plus the Domain's ingestion
mode setting (design 9.2): validation, `ignored_settings` and its audit entry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_core.reconcile import CandidateDraft, CandidateType, Claim
from lorebase_core.revision import Origin
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.commit import SchemaUpsert, commit
from lorebase_store.db import create_engine
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.repositories import ReconciliationCandidateRepository, scope_to_domain
from lorebase_store.settings import DatabaseSettings

from .conftest import PEPPER
from .test_proposals_postgres import (
    API,
    ATTRIBUTE,
    DOMAIN,
    LOADER,
    PROPOSALS,
    SCHEMA,
    SUPPLIER,
    Loader,
    proposal_body,
    setup_domain,
)

pytestmark = pytest.mark.integration

STEWARD_ROLE = "risk-steward"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
T0 = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
async def loader(database_url: str) -> AsyncIterator[Loader]:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        yield Loader(engine)
    finally:
        await engine.dispose()


def auth(admin_id: str) -> dict[str, Any]:
    return {
        "mode": "api_key",
        "api_keys": {"pepper": PEPPER},
        "installation_admins": [admin_id],
        "authz": {
            "grants_cache_ttl_s": 0,
            "invisible_kinds": True,
            "custom_roles": {STEWARD_ROLE: ["proposal.review", "conflict.resolve"]},
        },
    }


async def principal(loader: Loader, name: str, grants: list[dict[str, Any]]) -> tuple[str, Any]:
    """A service principal with `grants` (role and optional scope) and an API key."""
    store = SqlAuthStore(async_sessionmaker(loader.engine, expire_on_commit=False))
    async with store.unit() as unit:
        found = await unit.principals.add(PrincipalType.SERVICE, name)
        for grant in grants:
            await unit.grants.add(
                domain_id="supply", subject=found.id, created_by="usr_seed", **grant
            )
        generated = CODEC.generate()
        await unit.api_keys.add(
            principal_id=found.id, prefix=generated.prefix, key_hash=generated.key_hash
        )
    return found.id, {"Authorization": f"Bearer {generated.token}"}


async def declare_stewards(loader: Loader) -> None:
    """`domain.stewards: [reviewer]` and `Supplier.stewards: [risk-steward]` on main."""
    async with loader.session() as session:
        await commit(
            session,
            "supply",
            "main",
            [],
            schema_changes=[
                SchemaUpsert(SCHEMA.domain.model_copy(update={"stewards": ("reviewer",)})),
                SchemaUpsert(SUPPLIER.model_copy(update={"stewards": (STEWARD_ROLE,)})),
            ],
            author=LOADER,
            message="Declare stewards",
            origin=Origin(type="import"),
            runtime=loader.runtime,
        )


async def describe_supplier(loader: Loader, branch: str) -> None:
    """A schema-only change on `branch`."""
    async with loader.session() as session:
        await commit(
            session,
            "supply",
            branch,
            [],
            schema_changes=[
                SchemaUpsert(SUPPLIER.model_copy(update={"description": "Vendors of parts."}))
            ],
            author=LOADER,
            message="Describe Suppliers",
            origin=Origin(type="import"),
            runtime=loader.runtime,
        )


async def candidate(loader: Loader, key: str) -> str:
    async with loader.session() as session, session.begin():
        await scope_to_domain(session, "supply")
        outcome = await ReconciliationCandidateRepository(session, "supply").upsert(
            CandidateDraft(
                type=CandidateType.CONTRADICTION,
                branch="main",
                kind_id=SUPPLIER.id,
                keys=(key,),
                attribute_id=ATTRIBUTE["city"].id,
                claims=(
                    Claim(
                        claim_id="a",
                        value="Lyon",
                        source="source:src_a",
                        provenance_id="prv_a",
                        at=T0,
                    ),
                    Claim(
                        claim_id="b",
                        value="Nice",
                        source="source:src_b",
                        provenance_id="prv_b",
                        at=T0,
                    ),
                ),
            ),
            sensitivity=(),
            max_claims=20,
            now=T0,
            new_id=lambda: "rcn_00000000000000000000000001",
        )
    return outcome.row.id


async def forget_kinds(loader: Loader, proposal_id: str) -> None:
    """The Proposal as one opened before its Kinds were stored."""
    async with loader.session() as session, session.begin():
        await scope_to_domain(session, "supply")
        await session.execute(
            text("UPDATE proposals SET kinds = NULL WHERE id = :id"), {"id": proposal_id}
        )


def ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def test_stewards_route_work_and_gate_approvals(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper(auth=auth(installation_admin.id)) as http:
        await setup_domain(http, admin, loader, "sup", "doc")
        editor = await role_key(http, "supply", "editor")
        maintainer = await role_key(http, "supply", "maintainer")
        viewer = await role_key(http, "supply", "viewer")
        reviewer_id, reviewer = await principal(loader, "reviewer", [{"role": "reviewer"}])
        steward_id, steward = await principal(
            loader, "risk steward", [{"role": STEWARD_ROLE}, {"role": "viewer"}]
        )
        _narrow_id, narrow = await principal(
            loader,
            "part-only steward",
            [
                {"role": STEWARD_ROLE},
                {"role": "viewer", "scope_type": GrantScope.KIND, "scope_ref": "Part"},
            ],
        )
        await loader.edit("sup", "S-1001", city="Gdansk")
        await describe_supplier(loader, "doc")
        sup = (await http.post(PROPOSALS, json=proposal_body("sup"), headers=editor)).json()
        doc = (await http.post(PROPOSALS, json=proposal_body("doc"), headers=editor)).json()
        assert sup["id"]
        assert doc["id"]

        # No stewards declared: nothing changes.
        listed = (await http.get(PROPOSALS, headers=admin)).json()["items"]
        assert {item["id"]: item["steward_roles"] for item in listed} == {
            sup["id"]: [],
            doc["id"]: [],
        }
        assert ids(await http.get(PROPOSALS, params={"assigned": "me"}, headers=steward)) == []
        none = await http.get(PROPOSALS, params={"assigned": "none"}, headers=viewer)
        assert set(ids(none)) == {sup["id"], doc["id"]}

        # Kinds not stored yet (a Proposal from before they were): computed when read.
        await forget_kinds(loader, sup["id"])
        await declare_stewards(loader)
        everything = {
            i["id"]: i for i in (await http.get(PROPOSALS, headers=admin)).json()["items"]
        }
        assert everything[sup["id"]]["steward_roles"] == [STEWARD_ROLE]
        assert everything[sup["id"]]["assignees"] == [steward_id]
        assert everything[doc["id"]]["steward_roles"] == ["reviewer"]  # schema only: the Domain's
        assert everything[doc["id"]]["assignees"] == [reviewer_id]
        hidden = (await http.get(PROPOSALS, headers=viewer)).json()["items"]
        assert {i["assignees"] for i in hidden} == {None}  # no principal.read
        mine = await http.get(PROPOSALS, params={"assigned": "me"}, headers=steward)
        assert ids(mine) == [sup["id"]]
        assert ids(await http.get(PROPOSALS, params={"assigned": "me"}, headers=reviewer)) == [
            doc["id"]
        ]
        # Holds the role but cannot read Suppliers: never an assignee of Supplier work.
        assert ids(await http.get(PROPOSALS, params={"assigned": "me"}, headers=narrow)) == []
        assert ids(await http.get(PROPOSALS, params={"assigned": "none"}, headers=admin)) == []
        anyone = await http.get(PROPOSALS, params={"assigned": "any"}, headers=viewer)
        assert set(ids(anyone)) == {sup["id"], doc["id"]}
        assert (
            await http.get(PROPOSALS, params={"assigned": "x"}, headers=viewer)
        ).status_code == 422

        # Reconciliation candidates of Suppliers go to the Supplier stewards.
        found = await candidate(loader, "S-1002")
        queue = f"{DOMAIN}/reconciliation"
        assert ids(await http.get(queue, params={"assigned": "me"}, headers=steward)) == [found]
        assert ids(await http.get(queue, params={"assigned": "me"}, headers=reviewer)) == []
        detail = (await http.get(f"{queue}/{found}", headers=admin)).json()
        assert (detail["steward_roles"], detail["assignees"]) == ([STEWARD_ROLE], [steward_id])

    # One approval must come from a steward.
    async with keeper(
        auth=auth(installation_admin.id), proposals={"required_approvals_from_stewards": 1}
    ) as http:
        base = f"{PROPOSALS}/{sup['id']}"
        await forget_kinds(loader, sup["id"])
        approved = await http.post(f"{base}/reviews", json={"verdict": "approve"}, headers=reviewer)
        assert approved.status_code == 201, approved.text
        approval = approved.json()["approval"]
        assert (approval["required"], approval["approvals"]) == (1, 1)
        assert (approval["required_from_stewards"], approval["steward_approvals"]) == (1, 0)
        assert approval["satisfied"] is False
        refused = await http.post(f"{base}/merge", headers=maintainer)
        assert refused.status_code == 409
        assert refused.json()["code"] == "approvals-required"
        stewarded = await http.post(f"{base}/reviews", json={"verdict": "approve"}, headers=steward)
        assert stewarded.json()["approval"]["satisfied"] is True
        merged = await http.post(f"{base}/merge", headers=maintainer)
        assert merged.status_code == 200, merged.text

    # More steward approvals required than there are assignees: visible, not skipped.
    async with keeper(
        auth=auth(installation_admin.id), proposals={"required_approvals_from_stewards": 2}
    ) as http:
        base = f"{PROPOSALS}/{doc['id']}"
        detail = (await http.get(base, headers=viewer)).json()
        assert detail["approval"]["stewards_unavailable"] is True
        assert detail["approval"]["required"] == 2
        unavailable = await http.post(f"{base}/merge", headers=maintainer)
        assert unavailable.status_code == 409
        assert unavailable.json()["code"] == "stewards-unavailable"


async def test_required_roles_gate_merge(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper(
        auth=auth(installation_admin.id), proposals={"required_roles": [STEWARD_ROLE]}
    ) as http:
        await setup_domain(http, admin, loader, "sup", "doc")
        editor = await role_key(http, "supply", "editor")
        maintainer = await role_key(http, "supply", "maintainer")
        _plain_id, plain = await principal(loader, "plain reviewer", [{"role": "reviewer"}])
        holder_id, holder = await principal(
            loader, "role holder", [{"role": STEWARD_ROLE}, {"role": "viewer"}]
        )
        await loader.edit("sup", "S-1001", city="Gdansk")
        created = (await http.post(PROPOSALS, json=proposal_body("sup"), headers=editor)).json()
        base = f"{PROPOSALS}/{created['id']}"

        # Enough approvals, none from the role: refused, the role named.
        approved = await http.post(f"{base}/reviews", json={"verdict": "approve"}, headers=plain)
        assert approved.status_code == 201, approved.text
        approval = approved.json()["approval"]
        assert (approval["required"], approval["approvals"]) == (1, 1)
        assert (approval["required_roles"], approval["missing_roles"]) == (
            [STEWARD_ROLE],
            [STEWARD_ROLE],
        )
        assert approval["satisfied"] is False
        refused = await http.post(f"{base}/merge", headers=maintainer)
        assert refused.status_code == 409
        assert refused.json()["code"] == "approvals-required"
        assert STEWARD_ROLE in refused.json()["detail"]
        assert refused.json()["missing_roles"] == [STEWARD_ROLE]

        # A role holder approves: the merge goes through.
        by_holder = await http.post(f"{base}/reviews", json={"verdict": "approve"}, headers=holder)
        assert by_holder.status_code == 201, by_holder.text
        assert by_holder.json()["approval"]["missing_roles"] == []
        assert holder_id in by_holder.json()["approval"]["approvers"]
        merged = await http.post(f"{base}/merge", headers=maintainer)
        assert merged.status_code == 200, merged.text


async def test_domain_extraction_setting(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    url = f"{DOMAIN}"
    async with keeper() as http:
        await setup_domain(http, admin, loader)
        viewer = await role_key(http, "supply", "viewer")
        initial = (await http.get(url, headers=admin)).json()
        assert initial["ignored_settings"] == []
        assert initial["can"] == {"settings": True}
        assert (await http.get(url, headers=viewer)).json()["can"] == {"settings": False}
        assert all(
            d["can"] is None
            for d in (await http.get(f"{API}/domains", headers=admin)).json()["items"]
        )
        loose = {"extraction": {"mode": "direct", "auto_accept_threshold": 0.5}}
        denied = await http.patch(url, json={"settings": loose}, headers=viewer)
        assert denied.status_code == 403
        changed = await http.patch(url, json={"settings": loose}, headers=admin)
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert body["settings"]["extraction"] == loose["extraction"]
        assert body["ignored_settings"] == ["extraction.auto_accept_threshold"]
        assert body["can"] == {"settings": True}
        invalid = await http.patch(
            url, json={"settings": {"extraction": {"mode": "fast"}}}, headers=admin
        )
        assert invalid.status_code == 422
        assert invalid.json()["errors"][0]["pointer"] == "/body/settings/extraction/mode"
        other = await http.patch(url, json={"settings": {"merge_policy": {}}}, headers=admin)
        assert other.status_code == 422
        removed = await http.patch(url, json={"settings": {"extraction": None}}, headers=admin)
        assert "extraction" not in removed.json()["settings"]
    async with keeper(sections={"scribe": {"extraction": {"domain_may_loosen": []}}}) as http:
        await http.patch(url, json={"settings": loose}, headers=admin)
        strict = (await http.get(url, headers=admin)).json()
        assert strict["ignored_settings"] == [
            "extraction.mode",
            "extraction.auto_accept_threshold",
        ]
    async with loader.session() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT details FROM audit_log WHERE action = 'domain.update' "
                    "AND target = 'supply' ORDER BY seq"
                )
            )
        ).scalars()
        details = [dict(row) for row in rows]
    changes = [d["settings"] for d in details if d.get("settings")]
    assert changes[0] == [{"member": "extraction", "before": None, "after": loose["extraction"]}]
    assert changes[1] == [{"member": "extraction", "before": loose["extraction"], "after": None}]
