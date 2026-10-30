"""Corrections that stick (design 9.1, AC-34): a Source writes only what changed upstream, and
an upstream change that meets someone else's value is decided per `local_edits`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Link, LocalEdits, Schema
from lorebase_core.model.source import MergePolicy as SourcePolicy
from lorebase_core.validate import LinkUpsert
from lorebase_scribe.sources import SourceSync
from lorebase_store.merging import MergeRequest, MergeService
from lorebase_store.repositories import (
    ProposalRepository,
    ResolutionRepository,
    ResolutionRow,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime

from .support import (
    AUTO_MERGE,
    DOMAIN,
    Clock,
    commit_on,
    fetch,
    load_domain,
    record_change,
    rows,
    scalar,
    supply_schema,
)
from .test_source_sync import SUPPLIERS, rewrite, settings_for, suppliers_csv

pytestmark = pytest.mark.integration

__all__ = ["suppliers_csv"]  # the fixture


def schema_with(local_edits: LocalEdits | None, policy: SourcePolicy | None = None) -> Schema:
    schema = supply_schema({SUPPLIERS: policy} if policy else None)
    source = schema.source_by_api_name(SUPPLIERS)
    assert source is not None
    sources = {**schema.sources, source.id: source.model_copy(update={"local_edits": local_edits})}
    return schema.model_copy(update={"sources": sources})


def attribute_id(schema: Schema, name: str) -> str:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    found = kind.attribute_by_api_name(name)
    assert found is not None
    return found.id


async def value_on(
    sessions: async_sessionmaker[AsyncSession], schema: Schema, key: str, name: str
) -> Any:
    return await scalar(
        sessions,
        "SELECT values -> :a FROM mat_records WHERE domain_id = :d AND branch = 'main' "
        "AND key = :k",
        a=attribute_id(schema, name),
        d=DOMAIN,
        k=key,
    )


async def edit(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
    key: str,
    **values: str,
) -> None:
    """A person's edit of a Supplier on main (an Operation's commit)."""
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == key)
    await commit_on(sessions, runtime, clock, [record_change(kind, {**original, **values})])


def set_column(key: str, column: str, value: str) -> Any:
    def change(data: list[dict[str, str]]) -> list[dict[str, str]]:
        for row in data:
            if row["supplier_code"] == key:
                row[column] = value
        return data

    return change


async def audit(sessions: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    found = await fetch(
        sessions,
        "SELECT details FROM audit_log WHERE action = 'source.local_edit_conflict' ORDER BY seq",
    )
    return [row.details for row in found]


async def setup(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    csv_path: Path,
    schema: Schema,
) -> SourceSync:
    await load_domain(sessions, runtime, clock, schema)
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(csv_path.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=AUTO_MERGE,
    )
    first = await sync.run(DOMAIN, SUPPLIERS)
    assert first.proposal_status == "merged"
    assert first.seeded == 30
    return sync


async def test_an_unchanged_upstream_value_never_overwrites_a_correction(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(None)
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    # The merged run recorded what the Source wrote: hashes per Attribute, never values.
    assert await scalar(sessions, "SELECT count(*) FROM source_written") == 30
    assert await scalar(sessions, "SELECT count(*) FROM source_written_pending") == 0
    await edit(sessions, runtime, clock, schema, "S-1001", legal_name="Renamed")
    for _ in range(2):
        again = await sync.run(DOMAIN, SUPPLIERS)
        assert again.counts["changed"] == 0
        assert again.revisions == []
        assert again.local_edits == {}
    assert await value_on(sessions, schema, "S-1001", "legal_name") == "Renamed"
    # Another value of the same Record changes upstream: only that value is written.
    rewrite(suppliers_csv, set_column("S-1001", "city", "Turin"))
    changed = await sync.run(DOMAIN, SUPPLIERS)
    assert changed.counts["changed"] == 1
    assert changed.proposal_status == "merged"
    assert await value_on(sessions, schema, "S-1001", "legal_name") == "Renamed"
    assert await value_on(sessions, schema, "S-1001", "city") == "Turin"


async def test_an_upstream_change_over_a_correction_is_a_conflict_under_review(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(None)  # review is the default
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    original = await value_on(sessions, schema, "S-1003", "risk_score")
    await edit(sessions, runtime, clock, schema, "S-1003", risk_score="10")
    rewrite(suppliers_csv, set_column("S-1003", "risk_score", "71"))

    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.local_edits == {"conflict": 1}
    assert report.merge_status == "conflicted"
    assert report.proposal_status == "open"
    assert report.conflicts == 1
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "10"
    [conflict] = await fetch(
        sessions,
        "SELECT c.code, c.element_key, c.attribute_id, c.payload FROM conflicts c "
        "JOIN merge_attempts m ON m.domain_id = c.domain_id AND m.id = c.attempt_id "
        "WHERE m.proposal_id = :p AND m.status = 'conflicted'",
        p=report.proposal_id,
    )
    assert (conflict.code, conflict.element_key) == ("DV1", "S-1003")
    assert conflict.attribute_id == attribute_id(schema, "risk_score")
    # The base is what the Source last wrote, ours the correction, theirs the new value.
    assert conflict.payload["base"] == original
    assert (conflict.payload["ours"], conflict.payload["theirs"]) == ("10", "71")
    [entry] = await audit(sessions)
    assert entry["outcome"] == "conflict"
    assert entry["local_edits"] == "review"
    assert entry["key"] == "S-1003"
    assert entry["origin_type"] == "operation"
    assert entry["proposal"] == report.proposal_id
    assert "value" not in entry
    assert "10" not in entry.values()
    # The state advances only with the merge; only the changed element is carried.
    assert await scalar(sessions, "SELECT count(*) FROM source_written_pending") == 1

    again = await sync.run(DOMAIN, SUPPLIERS)
    assert again.proposal_id == report.proposal_id
    assert again.conflicts == 1
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "10"

    # A reviewer keeps the correction and merges: the state advances in the merge.
    fingerprint = await scalar(
        sessions,
        "SELECT encode(c.fingerprint, 'hex') FROM conflicts c JOIN merge_attempts m "
        "ON m.domain_id = c.domain_id AND m.id = c.attempt_id "
        "WHERE m.proposal_id = :p AND m.status = 'conflicted'",
        p=report.proposal_id,
    )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await ResolutionRepository(session, DOMAIN).upsert_many(
            str(report.proposal_id),
            [
                ResolutionRow(
                    fingerprint=fingerprint,
                    resolution={"choice": "ours"},
                    resolved_by="usr_reviewer",
                    at=clock(),
                )
            ],
        )
        proposal = await ProposalRepository(session, DOMAIN).get(str(report.proposal_id))
        assert proposal is not None
        merged = await MergeService(session, DOMAIN, runtime=runtime, clock=clock).merge(
            MergeRequest(proposal=proposal, actor="usr_reviewer")
        )
        assert merged.merged
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "10"
    assert await scalar(sessions, "SELECT count(*) FROM source_written_pending") == 0
    after = await sync.run(DOMAIN, SUPPLIERS)
    assert after.revisions == []
    assert after.local_edits == {}


async def test_keep_resolves_for_the_person(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(LocalEdits.KEEP)
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    await edit(sessions, runtime, clock, schema, "S-1003", risk_score="10")
    rewrite(suppliers_csv, set_column("S-1003", "risk_score", "71"))

    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.local_edits == {"kept": 1}
    assert report.proposal_status == "merged"
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "10"
    resolvers = await fetch(
        sessions,
        "SELECT resolved_by FROM conflict_resolutions WHERE proposal_id = :p",
        p=report.proposal_id,
    )
    assert [r.resolved_by for r in resolvers] == ["policy:local_edits"]
    assert [e["outcome"] for e in await audit(sessions)] == ["kept"]
    # The state advanced to the upstream value: the next run has nothing to do.
    again = await sync.run(DOMAIN, SUPPLIERS)
    assert again.revisions == []
    assert again.local_edits == {}
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "10"


async def test_source_wins_overwrites_and_audits(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(LocalEdits.SOURCE_WINS)
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    await edit(sessions, runtime, clock, schema, "S-1003", risk_score="10")
    rewrite(suppliers_csv, set_column("S-1003", "risk_score", "71"))

    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.local_edits == {"overwritten": 1}
    assert report.proposal_status == "merged"
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "71"
    assert [e["outcome"] for e in await audit(sessions)] == ["overwritten"]


async def test_a_snapshot_deletion_of_an_edited_record_is_a_conflict(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(None)
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    await edit(sessions, runtime, clock, schema, "S-1030", status="on_hold")
    rewrite(suppliers_csv, lambda data: [r for r in data if r["supplier_code"] != "S-1030"])

    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.counts["removed"] == 1
    assert report.local_edits == {"conflict": 1}
    assert report.merge_status == "conflicted"
    assert await value_on(sessions, schema, "S-1030", "status") == "on_hold"
    codes = await fetch(
        sessions,
        "SELECT c.code, c.element_key FROM conflicts c JOIN merge_attempts m "
        "ON m.domain_id = c.domain_id AND m.id = c.attempt_id "
        "WHERE m.proposal_id = :p AND m.status = 'conflicted'",
        p=report.proposal_id,
    )
    assert [(c.code, c.element_key) for c in codes] == [("DV2", "S-1030")]
    [entry] = await audit(sessions)
    assert (entry["attribute"], entry["outcome"]) == (None, "conflict")


async def link_part(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
    part: str,
    supplier: str,
) -> None:
    """A person adds a Part and its `supplied_by` Link to a Supplier on main."""
    kind = schema.kind_by_api_name("Part")
    found = schema.relation_by_api_name("supplied_by")
    assert kind is not None
    assert found is not None
    row = next(r for r in rows("part.csv") if r["part_number"] == part)
    link = Link(relation=found[0].id, from_key=part, to_key=supplier)
    await commit_on(sessions, runtime, clock, [record_change(kind, row), LinkUpsert(link=link)])


async def links_to(sessions: async_sessionmaker[AsyncSession], supplier: str) -> list[str]:
    found = await fetch(
        sessions,
        "SELECT from_key FROM mat_links WHERE domain_id = :d AND branch = 'main' "
        "AND to_key = :k AND NOT deleted ORDER BY from_key",
        d=DOMAIN,
        k=supplier,
    )
    return [row.from_key for row in found]


async def test_a_kept_record_keeps_its_links(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(LocalEdits.KEEP)
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    await link_part(sessions, runtime, clock, schema, "P-10039", "S-1030")
    await edit(sessions, runtime, clock, schema, "S-1030", status="on_hold")
    rewrite(suppliers_csv, lambda data: [r for r in data if r["supplier_code"] != "S-1030"])

    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.local_edits == {"kept": 1}
    assert report.proposal_status == "merged"
    # The deletion is not taken, and neither is the removal of the Links it carried.
    assert await value_on(sessions, schema, "S-1030", "status") == "on_hold"
    assert await links_to(sessions, "S-1030") == ["P-10039"]


async def test_a_deletion_taken_by_the_reviewer_removes_the_links(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(None)
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    await link_part(sessions, runtime, clock, schema, "P-10039", "S-1030")
    await edit(sessions, runtime, clock, schema, "S-1030", status="on_hold")
    rewrite(suppliers_csv, lambda data: [r for r in data if r["supplier_code"] != "S-1030"])
    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.merge_status == "conflicted"
    assert await links_to(sessions, "S-1030") == ["P-10039"]

    fingerprint = await scalar(
        sessions,
        "SELECT encode(c.fingerprint, 'hex') FROM conflicts c JOIN merge_attempts m "
        "ON m.domain_id = c.domain_id AND m.id = c.attempt_id "
        "WHERE m.proposal_id = :p AND m.status = 'conflicted'",
        p=report.proposal_id,
    )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await ResolutionRepository(session, DOMAIN).upsert_many(
            str(report.proposal_id),
            [
                ResolutionRow(
                    fingerprint=fingerprint,
                    resolution={"choice": "theirs"},
                    resolved_by="usr_reviewer",
                    at=clock(),
                )
            ],
        )
        proposal = await ProposalRepository(session, DOMAIN).get(str(report.proposal_id))
        assert proposal is not None
        merged = await MergeService(session, DOMAIN, runtime=runtime, clock=clock).merge(
            MergeRequest(proposal=proposal, actor="usr_reviewer")
        )
        assert merged.merged
    assert await value_on(sessions, schema, "S-1030", "status") is None
    assert await links_to(sessions, "S-1030") == []


async def test_keep_decides_again_a_conflict_a_person_reopened(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    # A Proposal left open (merged by people): a new edit of the kept value makes a new
    # Conflict, which the next run decides for the person again.
    schema = schema_with(LocalEdits.KEEP, SourcePolicy.PROPOSAL)
    await load_domain(sessions, runtime, clock, schema)
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=AUTO_MERGE,
    )
    await sync.run(DOMAIN, SUPPLIERS)
    proposal_id = await scalar(sessions, "SELECT id FROM proposals WHERE status = 'open'")
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        proposal = await ProposalRepository(session, DOMAIN).get(str(proposal_id))
        assert proposal is not None
        first = await MergeService(session, DOMAIN, runtime=runtime, clock=clock).merge(
            MergeRequest(proposal=proposal, actor="usr_reviewer")
        )
        assert first.merged
    await edit(sessions, runtime, clock, schema, "S-1003", risk_score="10")
    rewrite(suppliers_csv, set_column("S-1003", "risk_score", "71"))
    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.local_edits == {"kept": 1}
    assert (report.proposal_status, report.conflicts) == ("open", 0)

    await edit(sessions, runtime, clock, schema, "S-1003", risk_score="12")
    rewrite(suppliers_csv, set_column("S-1001", "city", "Turin"))
    again = await sync.run(DOMAIN, SUPPLIERS)
    assert again.proposal_id == report.proposal_id
    assert again.local_edits == {}
    assert again.conflicts == 0
    resolvers = await fetch(
        sessions,
        "SELECT resolved_by FROM conflict_resolutions WHERE proposal_id = :p",
        p=report.proposal_id,
    )
    assert [r.resolved_by for r in resolvers] == ["policy:local_edits"] * 2
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "12"


async def test_seeding_without_state(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(None)
    sync = await setup(sessions, runtime, clock, suppliers_csv, schema)
    # A Source that synced before the last-written state existed: values whose Lineage is
    # the Source seed the state (`W := C`), so a plain upstream change merges.
    await fetch(sessions, "DELETE FROM source_written RETURNING key")
    rewrite(suppliers_csv, set_column("S-1003", "risk_score", "71"))
    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.seeded == 30
    assert report.local_edits == {}
    assert report.proposal_status == "merged"
    assert await value_on(sessions, schema, "S-1003", "risk_score") == "71"
    assert await scalar(sessions, "SELECT count(*) FROM source_written") == 30


async def test_seeding_over_imported_data_compares_only(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = schema_with(None)
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    rewrite(suppliers_csv, set_column("S-1001", "legal_name", "Upstream Name"))
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=AUTO_MERGE,
    )
    dry = await sync.run(DOMAIN, SUPPLIERS, dry_run=True)
    assert dry.local_edits == {"conflict": 1}
    assert dry.seeded == 30

    report = await sync.run(DOMAIN, SUPPLIERS)
    # Imported values are someone else's: equal ones seed the state, the differing one is a
    # local edit.
    assert report.local_edits == {"conflict": 1}
    assert report.merge_status == "conflicted"
    assert await value_on(sessions, schema, "S-1001", "legal_name") != "Upstream Name"
