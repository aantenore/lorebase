"""The reconciliation queue through the API against Postgres (design 10.6): reads masked by
clearance, decisions written through `update_{kind}` with a `reconciliation` provenance, keep
current, dismissal and reopening, a decision proposed on the protected default branch and
settled by its merge, the blocking merge check, bulk dismissal and sweeps; one pending
decision at a time, withdrawal of a failed proposal, stale candidates and clearance of the
values a decision writes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from lorebase_core.model import Sensitivity
from lorebase_core.reconcile import CandidateDraft, CandidateType, Claim, DecisionAction
from lorebase_store import storage
from lorebase_store.db import create_engine
from lorebase_store.repositories import (
    CandidateStateError,
    ReconciliationCandidateRepository,
    scope_to_domain,
)
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

        # On the protected default branch the write is proposed, from a branch of its own
        # (one prepared under a predictable name is never reused); the merge resolves it.
        prepared = await http.post(
            f"{DOMAIN}/branches", json={"name": f"reconcile/{on_main}"}, headers=admin
        )
        assert prepared.status_code == 201, prepared.text
        proposed = await http.post(
            f"{QUEUE}/{on_main}/resolution",
            json={"choice": "claim", "claim_id": "prv_j", "reason": "confirmed by phone"},
            headers=maintainer,
        )
        assert proposed.status_code == 200, proposed.text
        body = proposed.json()
        assert body["status"] == "proposed"
        assert body["branch"].startswith(f"reconcile/{on_main}-")
        assert body["candidate"]["status"] == "open"
        pending = body["candidate"]["decision"]
        assert (pending["pending"], pending["branch"]) == (True, body["branch"])
        assert pending["head"] == body["revision"]
        proposal = body["proposal"]
        detail = (await http.get(f"{PROPOSALS}/{proposal}", headers=admin)).json()
        assert detail["source_ref"] == body["branch"]
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
        assert body["status"] == "proposed"
        assert body["branch"].startswith(f"reconcile/{candidate}-")
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


async def duplicate(loader: Loader, branch: str, *keys: str) -> str:
    async with loader.session() as session, session.begin():
        await scope_to_domain(session, "supply")
        outcome = await ReconciliationCandidateRepository(session, "supply").upsert(
            CandidateDraft(
                type=CandidateType.DUPLICATE, branch=branch, kind_id=SUPPLIER.id, keys=keys
            ),
            max_claims=20,
            now=T0,
            new_id=lambda: f"rcn_{next(IDS):026d}",
        )
    return outcome.row.id


async def decision_branches(loader: Loader, candidate: str) -> int:
    return int(
        await loader.scalar(
            "SELECT count(*) FROM refs WHERE domain_id = 'supply' AND name LIKE :p",
            p=f"reconcile/{candidate}-%",
        )
    )


async def decision_proposals(loader: Loader, candidate: str, status: str) -> int:
    return int(
        await loader.scalar(
            "SELECT count(*) FROM proposals WHERE domain_id = 'supply' AND status = :s "
            "AND source_ref LIKE :p",
            s=status,
            p=f"reconcile/{candidate}-%",
        )
    )


async def test_one_pending_decision_at_a_time_and_failed_proposals_are_withdrawn(
    keeper: Any,
    installation_admin: Any,
    role_key: Any,
    loader: Loader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        await setup_domain(http, admin, loader)
        maintainer = await role_key(http, "supply", "maintainer")
        first, second = list(loader.rows)[:2]
        candidate = await seed(
            loader,
            "main",
            first,
            claim("prv_a", city(loader, first), "source:src_erp", 0),
            claim("prv_b", "Lyon", "source:src_crm", 1),
        )
        url = f"{QUEUE}/{candidate}/resolution"
        decide = {"choice": "claim", "claim_id": "prv_b", "reason": "confirmed"}

        # Two decisions at once on the protected branch: one is proposed, the other refused
        # and its branch and Proposal (if it got that far) withdrawn.
        both = await asyncio.gather(
            http.post(url, json=decide, headers=maintainer),
            http.post(url, json=decide, headers=maintainer),
        )
        by_status = {r.status_code: r for r in both}
        assert sorted(by_status) == [200, 409], [r.text for r in both]
        proposed = by_status[200].json()
        assert proposed["status"] == "proposed"
        refused = by_status[409].json()
        assert (refused["code"], refused["proposal"]) == (
            "reconciliation-pending",
            proposed["proposal"],
        )
        assert await decision_branches(loader, candidate) == 1
        assert await decision_proposals(loader, candidate, "open") == 1

        # No other decision while that Proposal is open, not even one that writes nothing.
        kept = await http.post(
            url, json={"choice": "keep_current", "reason": "ERP"}, headers=maintainer
        )
        assert kept.status_code == 409, kept.text
        assert kept.json()["code"] == "reconciliation-pending"

        # Once it is closed, a new decision replaces the pending one.
        closed = await http.post(f"{PROPOSALS}/{proposed['proposal']}/close", headers=maintainer)
        assert closed.status_code == 200, closed.text
        again = await http.post(url, json=decide, headers=maintainer)
        assert again.status_code == 200, again.text
        assert again.json()["proposal"] != proposed["proposal"]
        assert again.json()["candidate"]["decision"]["proposal"] == again.json()["proposal"]

        # Recording the pending decision fails (the candidate was dismissed meanwhile): the
        # Proposal is closed, the branch deleted and the withdrawal audited.
        other = await seed(
            loader,
            "main",
            second,
            claim("prv_c", city(loader, second), "source:src_erp", 0),
            claim("prv_d", "Nice", "source:src_crm", 1),
        )

        async def dismissed(self: Any, candidate_id: str, *_args: Any, **_kwargs: Any) -> Any:
            raise CandidateStateError(candidate_id, "dismissed", DecisionAction.RESOLVE)

        monkeypatch.setattr(ReconciliationCandidateRepository, "set_pending", dismissed)
        failed = await http.post(
            f"{QUEUE}/{other}/resolution",
            json={"choice": "claim", "claim_id": "prv_d", "reason": "confirmed"},
            headers=maintainer,
        )
        assert failed.status_code == 409, failed.text
        assert failed.json()["code"] == "candidate-not-open"
        assert await decision_branches(loader, other) == 0
        assert await decision_proposals(loader, other, "open") == 0
        assert await decision_proposals(loader, other, "closed") == 1
        withdrawn = await loader.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'reconciliation.resolve' "
            "AND target = :t AND details ->> 'status' = 'withdrawn' "
            "AND details ->> 'outcome' = 'rejected'",
            t=other,
        )
        assert withdrawn == 1


async def test_stale_candidates_and_values_the_caller_may_not_read_are_refused(
    keeper: Any, installation_admin: Any, role_key: Any, loader: Loader
) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        await setup_domain(http, admin, loader, "side")
        maintainer = await role_key(http, "supply", "maintainer")
        first, second, third = list(loader.rows)[:3]

        # A third value was written since detection: claim and policy choices need a new one.
        moved = await seed(
            loader,
            "side",
            second,
            claim("prv_a", city(loader, second), "source:src_erp", 0),
            claim("prv_b", "Nice", "source:src_crm", 1),
        )
        await loader.edit("side", second, city="Brest")
        for body in (
            {"choice": "claim", "claim_id": "prv_b", "reason": "CRM"},
            {"choice": "policy", "policy": "newest_wins", "reason": "newest"},
        ):
            stale = await http.post(f"{QUEUE}/{moved}/resolution", json=body, headers=maintainer)
            assert stale.status_code == 409, stale.text
            assert stale.json()["code"] == "reconciliation-stale"
            assert "detection" in stale.json()["detail"]

        # A confidential value: written only by a caller cleared to read it.
        vat = loader.rows[first]["vat_number"]
        secret = await seed(
            loader,
            "side",
            first,
            claim("prv_c", vat, "source:src_erp", 0),
            claim("prv_d", "PL-OTHER", "source:src_crm", 1),
            attribute=VAT,
            tags=(Sensitivity.CONFIDENTIAL,),
        )
        url = f"{QUEUE}/{secret}/resolution"
        for body in (
            {"choice": "claim", "claim_id": "prv_d", "reason": "CRM"},
            {"choice": "custom", "value": "PL-NEW", "reason": "typed"},
        ):
            refused = await http.post(url, json=body, headers=maintainer)
            assert refused.status_code == 403, refused.text
        cleared = await http.post(
            url, json={"choice": "claim", "claim_id": "prv_d", "reason": "CRM"}, headers=admin
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["status"] == "resolved"

        # A duplicate merge may not write a value the caller cannot read, nor a placeholder.
        pair = await duplicate(loader, "side", first, third)
        merge_url = f"{QUEUE}/{pair}/resolution"
        merge = {"choice": "merge", "survivor": first, "reason": "same company"}
        sensitive = await http.post(
            merge_url, json={**merge, "values": {"vat_number": "PL-X"}}, headers=maintainer
        )
        assert sensitive.status_code == 403, sensitive.text
        masked = await http.post(
            merge_url, json={**merge, "values": {"city": "***"}}, headers=maintainer
        )
        assert masked.status_code == 422, masked.text
        assert masked.json()["errors"][0]["pointer"] == "/body/values/city"
        assert await decision_branches(loader, pair) == 0
