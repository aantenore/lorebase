"""Reconciliation candidates and provenance periods against Postgres (design 10.6, migrations
`0038` and `0039`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import ProvenanceEntry, Sensitivity, SourceSyncOrigin
from lorebase_core.reconcile import CandidateDraft, CandidateStatus, CandidateType, Claim
from lorebase_store.repositories import (
    CandidateFilter,
    CandidateStateError,
    ProvenanceRepository,
    ProvenanceRow,
    ReconciliationCandidateRepository,
    scope_to_domain,
)
from lorebase_store.settings import StoreSettings

pytestmark = pytest.mark.integration

DOMAIN = "supply"
NOW = datetime(2026, 10, 20, tzinfo=UTC)
KIND = "knd_01JB8Z00000000000000000001"
CITY = "att_01JB8Z00000000000000000001"
NAME = "att_01JB8Z00000000000000000002"
SOURCE = "src_01JB8Z00000000000000000001"
IDS = count(1)


def new_id() -> str:
    return f"rcn_{next(IDS):026d}"


@pytest.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO domains (id, display_name, leaf_size, created_at, created_by) "
                "VALUES (:id, :id, 512, :at, 'usr_test')"
            ),
            {"id": DOMAIN, "at": NOW},
        )
    return async_sessionmaker(engine, expire_on_commit=False)


def claim(claim_id: str, value: Any, source: str, days: int = 0) -> Claim:
    return Claim(
        claim_id=claim_id,
        value=value,
        source=source,
        provenance_id=claim_id,
        at=NOW + timedelta(days=days),
    )


def draft(*claims: Claim, attribute: str = CITY, key: str = "S-1", **extra: Any) -> CandidateDraft:
    return CandidateDraft(
        type=CandidateType.CONTRADICTION,
        branch="main",
        kind_id=KIND,
        keys=(key,),
        attribute_id=attribute,
        claims=claims,
        **extra,
    )


async def test_upsert_folds_claims_by_fingerprint(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = draft(claim("p1", "Milan", "source:a"), claim("p2", "Turin", "source:b", 1))
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repo = ReconciliationCandidateRepository(session, DOMAIN)
        created = await repo.upsert(
            first.model_copy(update={"signals": {"unit": True}}),
            max_claims=2,
            now=NOW,
            new_id=new_id,
        )
        assert created.created
        assert created.claims_added == 2
        assert created.row.status == CandidateStatus.OPEN.value
        assert created.row.claim_count == 2
        assert created.row.fingerprint == first.fingerprint

        # A third source repeating a known value: same candidate, one more claim, newest kept.
        again = await repo.upsert(
            draft(*first.claims, claim("p3", "Milan", "source:c", 2)).model_copy(
                update={"signals": {"negation": True}}
            ),
            max_claims=2,
            now=NOW + timedelta(hours=1),
            new_id=new_id,
        )
        assert not again.created
        assert again.row.id == created.row.id
        assert again.claims_added == 1
        assert again.row.claim_count == 3
        assert [c["claim_id"] for c in again.row.claims] == ["p2", "p3"]
        assert again.row.signals == {"unit": True, "negation": True}

        # A new value is a new candidate.
        other = await repo.upsert(
            draft(*first.claims, claim("p4", "Rome", "source:d", 3)),
            max_claims=5,
            now=NOW,
            new_id=new_id,
        )
        assert other.created
        assert other.row.id != created.row.id


async def test_decisions_follow_the_transitions(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repo = ReconciliationCandidateRepository(session, DOMAIN)
        row = (
            await repo.upsert(
                draft(claim("p1", "x", "source:a"), claim("p2", "y", "source:b")),
                max_claims=10,
                now=NOW,
                new_id=new_id,
            )
        ).row
        with pytest.raises(ValueError, match="reason"):
            await repo.dismiss(row.id, reason=" ", by="usr_1", at=NOW)
        dismissed = await repo.dismiss(row.id, reason="same city", by="usr_1", at=NOW)
        assert dismissed is not None
        assert dismissed.status == CandidateStatus.DISMISSED.value
        assert dismissed.decided_by == "usr_1"
        assert dismissed.decision is not None
        assert dismissed.decision["action"] == "dismiss"
        assert [c["claim_id"] for c in dismissed.decision["claims"]] == ["p1", "p2"]
        with pytest.raises(CandidateStateError):
            await repo.resolve(row.id, decision={}, reason="r", by="usr_1", at=NOW)

        # Seen again while dismissed: claims fold in, the status stays.
        kept = await repo.upsert(
            draft(claim("p1", "x", "source:a"), claim("p3", "y", "source:c")),
            max_claims=10,
            now=NOW,
            new_id=new_id,
        )
        assert kept.row.status == CandidateStatus.DISMISSED.value

        reopened = await repo.reopen(row.id, reason="new evidence", by="usr_2", at=NOW)
        assert reopened is not None
        assert reopened.status == CandidateStatus.OPEN.value
        assert reopened.decision is not None
        assert reopened.decision["previous"]["action"] == "dismiss"

        resolved = await repo.resolve(
            row.id,
            decision={"choice": "claim", "claim_id": "p1"},
            reason="ERP is authoritative",
            by="usr_2",
            at=NOW,
        )
        assert resolved is not None
        assert resolved.status == CandidateStatus.RESOLVED.value
        assert resolved.decision is not None
        assert resolved.decision["claim_id"] == "p1"
        with pytest.raises(CandidateStateError):
            await repo.reopen(row.id, reason="r", by="usr_2", at=NOW)
        assert await repo.dismiss("rcn_missing", reason="r", by="usr_1", at=NOW) is None


async def test_obsolete_candidates_open_again_when_seen(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repo = ReconciliationCandidateRepository(session, DOMAIN)
        candidate = draft(claim("p1", "x", "source:a"), claim("p2", "y", "source:b"))
        row = (await repo.upsert(candidate, max_claims=10, now=NOW, new_id=new_id)).row
        obsolete = await repo.mark_obsolete(
            row.id, reason="claims converged", by="svc_scribe", at=NOW
        )
        assert obsolete is not None
        assert obsolete.status == CandidateStatus.OBSOLETE.value
        back = await repo.upsert(candidate, max_claims=10, now=NOW, new_id=new_id)
        assert back.row.status == CandidateStatus.OPEN.value
        linked = await repo.link_proposal(row.id, "prp_1", now=NOW)
        assert linked is not None
        assert linked.proposal_id == "prp_1"


async def test_listing_filters_and_pages(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repo = ReconciliationCandidateRepository(session, DOMAIN)
        for i in range(3):
            await repo.upsert(
                draft(
                    claim(f"a{i}", f"x{i}", "source:a"),
                    claim(f"b{i}", f"y{i}", "source:b" if i else "source:z"),
                    key=f"S-{i}",
                    attribute=NAME if i == 2 else CITY,
                ),
                max_claims=10,
                now=NOW + timedelta(minutes=i),
                new_id=new_id,
            )
        everything = await repo.page(CandidateFilter(branch="main"), limit=10)
        assert [r.keys for r in everything] == [["S-2"], ["S-1"], ["S-0"]]
        first = await repo.page(CandidateFilter(), limit=2)
        rest = await repo.page(
            CandidateFilter(), limit=2, after=(first[-1].created_at, first[-1].id)
        )
        assert [r.id for r in [*first, *rest]] == [r.id for r in everything]
        by_source = await repo.page(CandidateFilter(source="source:z"), limit=10)
        assert [r.keys for r in by_source] == [["S-0"]]
        by_attribute = await repo.page(CandidateFilter(attribute_id=NAME), limit=10)
        assert [r.keys for r in by_attribute] == [["S-2"]]
        none = await repo.page(
            CandidateFilter(type=CandidateType.DUPLICATE, status=CandidateStatus.OPEN), limit=10
        )
        assert none == []
        assert await repo.page(CandidateFilter(kind_id=KIND, branch="dev"), limit=10) == []
        record = await repo.for_record("main", KIND, "S-1", statuses=[CandidateStatus.OPEN])
        assert [r.keys for r in record] == [["S-1"]]


async def test_proposal_checks_count_in_sql_and_list_the_first_ids(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repo = ReconciliationCandidateRepository(session, DOMAIN)
        on_source = []
        for index in range(3):
            stored = await repo.upsert(
                draft(
                    claim(f"s{index}a", "x", "source:a"),
                    claim(f"s{index}b", f"y{index}", "source:b"),
                    key=f"S-{index}",
                ).model_copy(update={"branch": "feature"}),
                max_claims=10,
                now=NOW + timedelta(minutes=index),
                new_id=new_id,
            )
            on_source.append(stored.row.id)
        raised = await repo.upsert(
            draft(claim("r1", "x", "source:a"), claim("r2", "z", "source:b"), key="S-9"),
            max_claims=10,
            now=NOW + timedelta(minutes=5),
            new_id=new_id,
        )
        await repo.upsert(
            CandidateDraft(
                type=CandidateType.DUPLICATE,
                branch="feature",
                kind_id=KIND,
                keys=("S-1", "S-2"),
                proposal_id="prp_1",
            ),
            max_claims=10,
            now=NOW + timedelta(minutes=6),
            new_id=new_id,
        )
        await repo.upsert(
            draft(claim("r3", "x", "source:a"), claim("r4", "w", "source:b"), key="S-8"),
            max_claims=10,
            now=NOW + timedelta(minutes=7),
            new_id=new_id,
        )
        # Raised by the Proposal, then decided through it: its merge resolves it, not counted.
        resolving = await repo.upsert(
            draft(claim("r5", "x", "source:a"), claim("r6", "v", "source:b"), key="S-7"),
            max_claims=10,
            now=NOW + timedelta(minutes=8),
            new_id=new_id,
        )
        await repo.set_pending(
            resolving.row.id, "prp_1", decision={"choice": "claim"}, reason="r", by="u", at=NOW
        )
        await session.execute(
            text("UPDATE reconciliation_candidates SET proposal_id = 'prp_1' WHERE id = :id"),
            {"id": raised.row.id},
        )
        checks = await repo.proposal_checks("prp_1", "feature", limit=2)
        assert checks.counts == {"contradiction": 4, "duplicate": 1}
        assert checks.open == 5
        assert checks.ids == on_source[:2]
        only = await repo.proposal_checks("prp_1", "feature", types=[CandidateType.DUPLICATE])
        assert only.counts == {"duplicate": 1}
        assert len(only.ids) == 1
        empty = await repo.proposal_checks("prp_2", "other")
        assert (empty.counts, empty.ids) == ({}, [])


async def test_lookups_by_proposal_and_key_have_indexes(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        found = await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'reconciliation_candidates'"
            )
        )
        indexes = {str(name): str(definition) for name, definition in found}
        assert (
            "(domain_id, proposal_id)"
            in indexes["ix_reconciliation_candidates_domain_id_proposal_id"]
        )
        assert "gin (keys jsonb_path_ops)" in indexes["ix_reconciliation_candidates_keys"]
        # The GIN index answers the containment test of the Record page.
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = await session.execute(
            text(
                "EXPLAIN SELECT id FROM reconciliation_candidates WHERE keys @> '[\"S-1\"]'::jsonb"
            )
        )
        assert "ix_reconciliation_candidates_keys" in "\n".join(plan.scalars())


async def test_sensitive_claim_values_are_wrapped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repo = ReconciliationCandidateRepository(session, DOMAIN)
        tagged = await repo.upsert(
            draft(claim("p1", "a@x.io", "source:a"), claim("p2", "b@x.io", "source:b")),
            sensitivity=(Sensitivity.PII,),
            max_claims=10,
            now=NOW,
            new_id=new_id,
        )
        assert tagged.row.claims[0]["value"] == {"$sensitive": {"tags": ["pii"], "value": "a@x.io"}}
        posing = await repo.upsert(
            draft(
                claim("p3", {"$vault": "vlt_x"}, "source:a"),
                claim("p4", "plain", "source:b"),
                attribute=NAME,
            ),
            max_claims=10,
            now=NOW,
            new_id=new_id,
        )
        values = [c["value"] for c in posing.row.claims]
        assert values[0] == {
            "$sensitive": {
                "tags": sorted(t.value for t in Sensitivity),
                "value": {"$vault": "vlt_x"},
            }
        }
        assert values[1] == "plain"


async def test_rows_are_scoped_to_their_domain(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repo = ReconciliationCandidateRepository(session, DOMAIN)
        row = (
            await repo.upsert(
                draft(claim("p1", "x", "source:a"), claim("p2", "y", "source:b")),
                max_claims=10,
                now=NOW,
                new_id=new_id,
            )
        ).row
    async with sessions() as session, session.begin():
        await scope_to_domain(session, "other")
        assert await ReconciliationCandidateRepository(session, "other").get(row.id) is None


async def test_provenance_periods_round_trip(sessions: async_sessionmaker[AsyncSession]) -> None:
    entry = ProvenanceEntry(
        id="prv_01JB8Z00000000000000000009",
        origin=SourceSyncOrigin(source_id=SOURCE, run_id="r1"),
        actor="svc_scribe",
        at=NOW,
        valid_from="2026-01-01T00:00:00Z",
    )
    plain = entry.model_copy(update={"id": "prv_01JB8Z00000000000000000008", "valid_from": None})
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        provenance = ProvenanceRepository(session, DOMAIN, writes=StoreSettings().writes)
        await provenance.insert_many([ProvenanceRow.from_entry(e) for e in (entry, plain)])
        found = await provenance.get_many([entry.id, plain.id])
    assert found[entry.id].valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert found[entry.id].valid_to is None
    assert found[plain.id].valid_from is None
