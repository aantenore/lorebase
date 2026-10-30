"""Ingestion modes of extraction (design 9.2 "Ingestion modes", AC-36) on the `supply` example.

The installation runs tier 0 with `auto_accept_threshold` 0.55, as the extraction tests do:
the review document then accepts some Links and leaves the rest pending, and the contract
document proposes C-3025 with values below the threshold.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.parsing import MARKDOWN
from lorebase_scribe.extraction.pipeline import ExtractionReport, ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.approval_rules import ApprovalRuleSettings
from lorebase_store.blobs import PostgresBlobStore
from lorebase_store.models.enums import ItemDecision, ProposalStatus
from lorebase_store.repositories import (
    DocumentRepository,
    DocumentRow,
    DomainRepository,
    ItemRow,
    ProposalRepository,
    RefRepository,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime

from .extraction_support import DOCUMENTS, proposal_items, seed_supply
from .support import DOMAIN, Clock, supply_schema
from .test_extraction import CONTRACT, REVIEW, THRESHOLD, extract

pytestmark = pytest.mark.integration


@pytest.fixture
async def schema(
    sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime, clock: Clock
) -> Schema:
    schema = supply_schema()
    await seed_supply(sessions, runtime, clock, schema)
    return schema


def run_with(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    approval_rules: ApprovalRuleSettings | None = None,
    **settings: Any,
) -> ExtractionRun:
    return ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(tiers=(0,), auto_accept_threshold=THRESHOLD, **settings),
        InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
        approval_rules=approval_rules,
    )


async def set_extraction(
    sessions: async_sessionmaker[AsyncSession], extraction: dict[str, Any]
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        domains = DomainRepository(session)
        domain = await domains.get(DOMAIN)
        assert domain is not None
        await domains.update(DOMAIN, settings={**domain.settings, "extraction": extraction})


async def update_domain(
    sessions: async_sessionmaker[AsyncSession], settings: dict[str, Any]
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        domains = DomainRepository(session)
        domain = await domains.get(DOMAIN)
        assert domain is not None
        await domains.update(DOMAIN, settings={**domain.settings, **settings})


async def protect_main(sessions: async_sessionmaker[AsyncSession], protected: bool) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await RefRepository(session, DOMAIN).set_protected("main", protected)


async def store_keyed(sessions: async_sessionmaker[AsyncSession], name: str, key: str) -> str:
    data = await asyncio.to_thread((DOCUMENTS / name).read_bytes)
    digest = hashlib.sha256(data).digest()
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        ref = await PostgresBlobStore().put(session, DOMAIN, data)
        outcome = await DocumentRepository(session, DOMAIN).add_version(
            DocumentRow(
                id=f"doc_{digest.hex()[:20]}{len(key):06d}",
                filename=name,
                media_type=MARKDOWN,
                sha256=digest,
                size=len(data),
                blob_ref=ref,
                created_by="usr_loader",
                source_key=key,
            )
        )
    return outcome.row.id


async def proposal_status(
    sessions: async_sessionmaker[AsyncSession], report: ExtractionReport
) -> ProposalStatus:
    assert report.proposal_id is not None
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        proposal = await ProposalRepository(session, DOMAIN).get(report.proposal_id)
    assert proposal is not None
    return proposal.status


async def merge_audits(sessions: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with sessions() as session, session.begin():
        rows = await session.execute(
            text(
                "SELECT details FROM audit_log WHERE action = 'proposal.merge' "
                "AND domain_id = :d ORDER BY seq"
            ),
            {"d": DOMAIN},
        )
        return [dict(r[0]) for r in rows]


def decisions(items: list[ItemRow]) -> list[ItemDecision]:
    return [i.decision for i in items]


async def test_without_a_domain_setting_extraction_is_unchanged(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    report = await extract(run_with(sessions, runtime, clock), sessions, REVIEW)
    assert report.as_dict()["mode"] == "doubts_only"
    assert (report.mode_capped, report.direct_blocked, report.direct_merged) == (
        None,
        None,
        False,
    )
    assert report.accepted > 0
    assert report.pending > 0
    assert report.ignored_settings == []
    assert await proposal_status(sessions, report) is ProposalStatus.OPEN


async def test_review_all_accepts_nothing_at_once(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    await set_extraction(sessions, {"mode": "review_all"})
    report = await extract(run_with(sessions, runtime, clock), sessions, REVIEW)
    assert report.mode == "review_all"
    assert report.accepted == 0
    assert report.pending == report.items > 0
    assert report.proposal_id is not None
    items = await proposal_items(sessions, report.proposal_id)
    assert set(decisions(items)) == {ItemDecision.PENDING}


async def test_direct_merges_a_tier_0_job_with_nothing_pending(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    # A threshold low enough that the contract's items are all accepted at once; loosening the
    # threshold is allowed here by the installation.
    await set_extraction(sessions, {"mode": "direct", "auto_accept_threshold": 0.0})
    run = run_with(sessions, runtime, clock, domain_may_loosen=("mode", "auto_accept_threshold"))
    report = await extract(run, sessions, CONTRACT)
    assert report.pending == 0
    assert report.accepted > 0
    assert (report.mode, report.mode_capped, report.direct_blocked) == ("direct", None, None)
    assert report.direct_merged is True
    assert report.merged_revision is not None
    assert await proposal_status(sessions, report) is ProposalStatus.MERGED
    audits = await merge_audits(sessions)
    assert audits[-1]["mode"] == "direct"
    assert audits[-1]["job"] == report.job_id
    assert audits[-1]["revision"] == report.merged_revision


async def test_direct_waits_for_pending_items(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    await set_extraction(sessions, {"mode": "direct"})
    report = await extract(run_with(sessions, runtime, clock), sessions, REVIEW)
    assert report.pending > 0
    assert (report.mode, report.direct_blocked, report.direct_merged) == (
        "direct",
        "pending_items",
        False,
    )
    assert await proposal_status(sessions, report) is ProposalStatus.OPEN
    assert await merge_audits(sessions) == []


async def test_direct_into_a_protected_branch_needs_the_installation(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    await protect_main(sessions, True)
    await set_extraction(sessions, {"mode": "direct", "auto_accept_threshold": 0.0})
    run = run_with(
        sessions,
        runtime,
        clock,
        domain_may_loosen=("mode", "auto_accept_threshold"),
        direct_merge_protected=False,
    )
    report = await extract(run, sessions, CONTRACT)
    assert report.direct_blocked == "protected"
    assert await proposal_status(sessions, report) is ProposalStatus.OPEN


@pytest.mark.parametrize(
    ("rules", "domain_settings", "held_by"),
    [
        (
            ApprovalRuleSettings(required_approvals_from_stewards=1),
            {},
            ["required_approvals_from_stewards"],
        ),
        (ApprovalRuleSettings(required_roles=["data_steward"]), {}, ["required_roles"]),
        (ApprovalRuleSettings(), {"required_roles": ["data_steward"]}, ["required_roles"]),
    ],
    ids=["stewards", "roles", "domain-roles"],
)
async def test_direct_waits_for_the_approvers_a_protected_target_names(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
    rules: ApprovalRuleSettings,
    domain_settings: dict[str, Any],
    held_by: list[str],
) -> None:
    await protect_main(sessions, True)
    await set_extraction(sessions, {"mode": "direct", "auto_accept_threshold": 0.0})
    if domain_settings:
        await update_domain(sessions, domain_settings)
    loosen = ("mode", "auto_accept_threshold")
    run = run_with(sessions, runtime, clock, rules, domain_may_loosen=loosen)
    report = await extract(run, sessions, CONTRACT)
    assert (report.direct_blocked, report.blocking_rules, report.direct_merged) == (
        "approvals",
        held_by,
        False,
    )
    assert report.as_dict()["blocking_rules"] == held_by
    assert await proposal_status(sessions, report) is ProposalStatus.OPEN
    assert await merge_audits(sessions) == []


async def test_the_approvals_count_alone_does_not_hold_a_direct_merge(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    await protect_main(sessions, True)
    await set_extraction(sessions, {"mode": "direct", "auto_accept_threshold": 0.0})
    await update_domain(sessions, {"required_approvals": 3})
    loosen = ("mode", "auto_accept_threshold")
    run = run_with(sessions, runtime, clock, domain_may_loosen=loosen)
    report = await extract(run, sessions, CONTRACT)
    assert (report.direct_blocked, report.direct_merged) == (None, True)
    assert await proposal_status(sessions, report) is ProposalStatus.MERGED


async def test_loosening_is_ignored_unless_the_installation_allows_it(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    await set_extraction(
        sessions, {"mode": "direct", "auto_accept_threshold": 0.0, "min_confidence": 0.1}
    )
    report = await extract(run_with(sessions, runtime, clock), sessions, CONTRACT)
    # The mode may loosen by default; the threshold may not, so the items stay pending.
    assert report.ignored_settings == ["extraction.auto_accept_threshold"]
    assert report.pending > 0
    assert report.direct_blocked == "pending_items"
    forbidden = run_with(sessions, runtime, clock, domain_may_loosen=())
    again = await extract(forbidden, sessions, REVIEW)
    assert again.mode == "doubts_only"
    assert again.ignored_settings == [
        "extraction.auto_accept_threshold",
        "extraction.mode",
    ]


async def test_rules_by_source_key_prefix(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    await set_extraction(
        sessions,
        {
            "rules": [
                {"source_key_prefix": "reviews/", "mode": "review_all"},
                {"source_key_prefix": "reviews/trusted/", "auto_accept_threshold": 0.95},
            ]
        },
    )
    run = run_with(sessions, runtime, clock)
    held = await run.run(DOMAIN, [await store_keyed(sessions, REVIEW, "reviews/2025/q4.md")])
    assert (held.mode, held.accepted) == ("review_all", 0)
    # The longest prefix wins; members it leaves out come from the Domain.
    trusted = await run.run(DOMAIN, [await store_keyed(sessions, REVIEW, "reviews/trusted/q4.md")])
    assert trusted.mode == "doubts_only"
    assert trusted.accepted == 0  # 0.95 is above every confidence of the review
    keyless = await extract(run, sessions, REVIEW)
    assert keyless.mode == "doubts_only"
    assert keyless.accepted > 0
