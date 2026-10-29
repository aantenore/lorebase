"""The reconciliation queue through the API against Postgres (design 10.6): reads masked by
clearance, decisions written through `update_{kind}` with a `reconciliation` provenance, keep
current, dismissal and reopening, a decision proposed on the protected default branch and
settled by its merge, the blocking merge check, bulk dismissal and sweeps."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from lorebase_core.model import Sensitivity
from lorebase_core.reconcile import CandidateDraft, CandidateType, Claim
from lorebase_store import storage
from lorebase_store.db import create_engine
from lorebase_store.repositories import ReconciliationCandidateRepository, scope_to_domain
from lorebase_store.settings import DatabaseSettings, StorageSettings, StoreSettings

from .test_proposals_postgres import (
    ATTRIBUTE,
    DOMAIN,
    PROPOSALS,
    SUPPLIER,
    Loader,
    proposal_body,
    setup_domain,
)

pytestmark = pytest.mark.integration

QUEUE = f"{DOMAIN}/reconciliation"
CITY = ATTRIBUTE["city"].id
VAT = ATTRIBUTE["vat_number"].id
T0 = datetime(2026, 9, 1, tzinfo=UTC)
IDS = count(1)
EVIDENCE = {"document_id": "doc_1", "chunk": 0, "start": 4, "end": 12, "quote": "VAT PL-SECRET"}


@pytest.fixture
async def loader(database_url: str) -> AsyncIterator[Loader]:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    store = StoreSettings(storage=StorageSettings(profile="balanced"))
    try:
        report = await storage.apply(engine, store, switch_wait_s=0)
        assert report.complete, report.as_dict()
        yield Loader(engine, store)
    finally:
        await engine.dispose()


def claim(claim_id: str, value: Any, source: str, days: int, **extra: Any) -> Claim:
    return Claim(
        claim_id=claim_id,
        value=value,
        source=source,
        provenance_id=claim_id,
        at=T0 + timedelta(days=days),
        **extra,
    )


async def seed(
    loader: Loader,
    branch: str,
    key: str,
    *claims: Claim,
    attribute: str = CITY,
    tags: tuple[Sensitivity, ...] = (),
) -> str:
    async with loader.session() as session, session.begin():
        await scope_to_domain(session, "supply")
        outcome = await ReconciliationCandidateRepository(session, "supply").upsert(
            CandidateDraft(
                type=CandidateType.CONTRADICTION,
                branch=branch,
                kind_id=SUPPLIER.id,
                keys=(key,),
                attribute_id=attribute,
                claims=claims,
            ),
            sensitivity=tags,
            max_claims=20,
            now=T0,
            new_id=lambda: f"rcn_{next(IDS):026d}",
        )
    return outcome.row.id


def city(loader: Loader, code: str) -> str:
    return loader.rows[code]["city"]


async def test_reconciliation_queue_end_to_end(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        await setup_domain(http, admin, loader, "side", "left")
        maintainer = await role_key(http, "supply", "maintainer")
        reviewer = await role_key(http, "supply", "reviewer")
        viewer = await role_key(http, "supply", "viewer")
        codes = list(loader.rows)
        first, second, third = codes[:3]
        moved = await seed(
            loader,
            "side",
            first,
            claim("prv_a", city(loader, first), "source:src_erp", 0),
            claim("prv_b", "Lyon", "source:src_crm", 2),
        )
        kept = await seed(
            loader,
            "side",
            second,
            claim("prv_c", city(loader, second), "source:src_erp", 0),
            claim("prv_d", "Nice", "source:src_crm", 1),
        )
        dismissed = await seed(
            loader,
            "side",
            third,
            claim("prv_e", city(loader, third), "source:src_erp", 0),
            claim("prv_f", "Metz", "source:src_crm", 1),
        )
        secret = await seed(
            loader,
            "side",
            first,
            claim("prv_g", "PL-SECRET", "document:doc_1", 0, evidence=EVIDENCE),
            claim("prv_h", "PL-OTHER", "source:src_crm", 1),
            attribute=VAT,
            tags=(Sensitivity.CONFIDENTIAL,),
        )
        on_main = await seed(
            loader,
            "main",
            first,
            claim("prv_i", city(loader, first), "source:src_erp", 0),
            claim("prv_j", "Krakow", "source:src_crm", 1),
        )

        # Reads: masked for a viewer, plain for the cleared administrator.
        listed = await http.get(QUEUE, params={"branch": "side"}, headers=viewer)
        assert listed.status_code == 200, listed.text
        items = {i["id"]: i for i in listed.json()["items"]}
        assert set(items) == {moved, kept, dismissed, secret}
        hidden = items[secret]
        assert (hidden["kind"], hidden["attribute"]) == ("Supplier", "vat_number")
        assert "PL-SECRET" not in listed.text
        assert hidden["claims"][0]["evidence"]["start"] == 4
        cleared = await http.get(f"{QUEUE}/{secret}", headers=admin)
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["claims"][0]["value"] == "PL-SECRET"
        assert cleared.json()["claims"][0]["evidence"]["quote"] == "VAT PL-SECRET"
        by_source = await http.get(
            QUEUE, params={"branch": "side", "source": "document:doc_1"}, headers=viewer
        )
        assert [i["id"] for i in by_source.json()["items"]] == [secret]
        paged = await http.get(QUEUE, params={"branch": "side", "limit": 1}, headers=viewer)
        assert paged.json()["next_cursor"]
        assert (await http.get(f"{QUEUE}/rcn_missing", headers=viewer)).status_code == 404

        # A decision that writes needs the write's own permission too.
        resolution = {"choice": "policy", "policy": "newest_wins", "reason": "CRM is current"}
        refused = await http.post(f"{QUEUE}/{moved}/resolution", json=resolution, headers=viewer)
        assert refused.status_code == 403
        assert refused.json()["permission"] == "conflict.resolve"
        no_write = await http.post(f"{QUEUE}/{moved}/resolution", json=resolution, headers=reviewer)
        assert no_write.status_code == 403
        assert no_write.json()["permission"] == "operation.execute"
        dry = await http.post(
            f"{QUEUE}/{moved}/resolution", json={**resolution, "dry_run": True}, headers=maintainer
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["status"] == "dry_run"
        assert dry.json()["candidate"]["status"] == "open"
        done = await http.post(f"{QUEUE}/{moved}/resolution", json=resolution, headers=maintainer)
        assert done.status_code == 200, done.text
        decided = done.json()
        assert (decided["status"], decided["claim_id"]) == ("resolved", "prv_b")
        assert decided["revision"]
        assert decided["candidate"]["status"] == "resolved"
        assert decided["candidate"]["decision"]["policy"] == "newest_wins"
        record = await http.get(
            f"{DOMAIN}/kinds/Supplier/records/{first}", params={"branch": "side"}, headers=admin
        )
        assert record.json()["record"]["values"]["city"] == "Lyon"
        assert (
            await loader.scalar(
                "SELECT count(*) FROM provenance WHERE origin_type = 'reconciliation' "
                "AND origin_ref ->> 'candidate_id' = :id AND origin_ref ->> 'claim_id' = 'prv_b' "
                "AND origin_ref ->> 'resolver' = 'policy:newest_wins'",
                id=moved,
            )
            == 1
        )

        # keep_current writes nothing; a decided candidate cannot be decided again.
        keep = await http.post(
            f"{QUEUE}/{kept}/resolution",
            json={"choice": "keep_current", "reason": "ERP is the master"},
            headers=reviewer,
        )
        assert keep.status_code == 200, keep.text
        assert (keep.json()["status"], keep.json()["claim_id"]) == ("resolved", "prv_c")
        assert keep.json()["revision"] is None
        again = await http.post(
            f"{QUEUE}/{kept}/dismissal", json={"reason": "late"}, headers=reviewer
        )
        assert again.status_code == 409
        assert again.json()["code"] == "candidate-not-open"

        # Dismissal and reopening.
        gone = await http.post(
            f"{QUEUE}/{dismissed}/dismissal", json={"reason": "both true"}, headers=reviewer
        )
        assert gone.json()["candidate"]["status"] == "dismissed"
        back = await http.post(
            f"{QUEUE}/{dismissed}/reopen", json={"reason": "new evidence"}, headers=reviewer
        )
        assert back.json()["candidate"]["status"] == "open"
        bulk = await http.post(
            f"{QUEUE}:resolve",
            json={"ids": [dismissed], "choice": "dismiss", "reason": "bulk"},
            headers=reviewer,
        )
        assert bulk.status_code == 200, bulk.text
        assert bulk.json()["counts"] == {"dismissed": 1}
        audited = await loader.scalar(
            "SELECT count(*) FROM audit_log WHERE action IN ('reconciliation.resolve', "
            "'reconciliation.dismiss', 'reconciliation.reopen') "
            "AND target IN (:a, :b, :c)",
            a=moved,
            b=kept,
            c=dismissed,
        )
        assert audited == 5  # resolve, resolve, dismiss, reopen, dismiss

        # On the protected default branch the write is proposed; the merge resolves it.
        proposed = await http.post(
            f"{QUEUE}/{on_main}/resolution",
            json={"choice": "claim", "claim_id": "prv_j", "reason": "confirmed by phone"},
            headers=maintainer,
        )
        assert proposed.status_code == 200, proposed.text
        body = proposed.json()
        assert body["status"] == "proposed"
        assert body["branch"] == f"reconcile/{on_main}"
        assert body["candidate"]["status"] == "open"
        proposal = body["proposal"]
        detail = (await http.get(f"{PROPOSALS}/{proposal}", headers=admin)).json()
        check = next(c for c in detail["checks"] if c["name"] == "reconciliation")
        assert check["status"] == "passed"
        approved = await http.post(
            f"{PROPOSALS}/{proposal}/reviews", json={"verdict": "approve"}, headers=reviewer
        )
        assert approved.status_code == 201, approved.text
        merged = await http.post(f"{PROPOSALS}/{proposal}/merge", headers=maintainer)
        assert merged.status_code == 200, merged.text
        settled = (await http.get(f"{QUEUE}/{on_main}", headers=viewer)).json()
        assert settled["status"] == "resolved"
        assert settled["decision"]["proposal"] == proposal

        sweep = await http.post(f"{QUEUE}/sweeps", json={"branch": "side"}, headers=maintainer)
        assert sweep.status_code == 202, sweep.text
        assert sweep.json()["branch"] == "side"
        assert (await http.post(f"{QUEUE}/sweeps", headers=viewer)).status_code == 403

    # In `block` mode an open contradiction on the source branch stops the merge.
    blocking = await seed(
        loader,
        "left",
        second,
        claim("prv_k", city(loader, second), "source:src_erp", 0),
        claim("prv_l", "Brest", "source:src_crm", 1),
    )
    await loader.edit("left", second, city="Brest")
    async with keeper(reconciliation={"merge_check": "block"}) as http:
        created = await http.post(PROPOSALS, json=proposal_body("left"), headers=maintainer)
        assert created.status_code == 201, created.text
        left = created.json()
        check = next(c for c in left["checks"] if c["name"] == "reconciliation")
        assert (check["status"], check["candidates"]) == ("failed", [blocking])
        await http.post(
            f"{PROPOSALS}/{left['id']}/reviews", json={"verdict": "approve"}, headers=reviewer
        )
        held = await http.post(f"{PROPOSALS}/{left['id']}/merge", headers=maintainer)
        assert held.status_code == 409, held.text
        assert held.json()["code"] == "reconciliation-open"
        assert held.json()["candidates"] == [blocking]
        await http.post(
            f"{QUEUE}/{blocking}/dismissal", json={"reason": "Brest is right"}, headers=reviewer
        )
        assert (
            await http.post(f"{PROPOSALS}/{left['id']}/merge", headers=maintainer)
        ).status_code == 200


async def test_duplicates_merge_through_a_proposal(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        await setup_domain(http, admin, loader, "side")
        maintainer = await role_key(http, "supply", "maintainer")
        survivor, absorbed = list(loader.rows)[:2]
        async with loader.session() as session, session.begin():
            await scope_to_domain(session, "supply")
            outcome = await ReconciliationCandidateRepository(session, "supply").upsert(
                CandidateDraft(
                    type=CandidateType.DUPLICATE,
                    branch="side",
                    kind_id=SUPPLIER.id,
                    keys=(survivor, absorbed),
                ),
                max_claims=20,
                now=T0,
                new_id=lambda: f"rcn_{next(IDS):026d}",
            )
        candidate = outcome.row.id
        wrong = await http.post(
            f"{QUEUE}/{candidate}/resolution",
            json={"choice": "keep_current", "reason": "same"},
            headers=maintainer,
        )
        assert wrong.status_code == 422
        merged = await http.post(
            f"{QUEUE}/{candidate}/resolution",
            json={
                "choice": "merge",
                "survivor": survivor,
                "values": {"city": "Szczecin"},
                "reason": "same company",
            },
            headers=maintainer,
        )
        assert merged.status_code == 200, merged.text
        body = merged.json()
        assert (body["status"], body["branch"]) == ("proposed", f"reconcile/{candidate}")
        assert body["candidate"]["decision"]["survivor"] == survivor
        done = await http.post(f"{PROPOSALS}/{body['proposal']}/merge", headers=maintainer)
        assert done.status_code == 200, done.text
        record = f"{DOMAIN}/kinds/Supplier/records"
        gone = await http.get(f"{record}/{absorbed}", params={"branch": "side"}, headers=admin)
        assert gone.status_code == 404
        kept = await http.get(f"{record}/{survivor}", params={"branch": "side"}, headers=admin)
        assert kept.json()["record"]["values"]["city"] == "Szczecin"
        settled = (await http.get(f"{QUEUE}/{candidate}", headers=admin)).json()
        assert settled["status"] == "resolved"
