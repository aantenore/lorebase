"""Proposal reads are authorized against both branches, and Kind grants limit the Conflicts;
Proposal creation and Conflict resolution keep that scope."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, relation_partition
from lorebase_keeper.auth.config import AuthzSettings
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import Authorizer
from lorebase_keeper.errors import ProblemError
from lorebase_keeper.reading.sensitivity import UNCLEARED
from lorebase_keeper.services.proposal_reads import (
    NO_SCOPE,
    ConflictScope,
    ConflictVisibility,
    conflict_scope,
    readable_proposals,
    require_target_read,
    require_visible,
    unknown_conflict,
    visible_partitions,
    visible_scope,
)
from lorebase_keeper.services.proposals import limited_summary
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.models.enums import (
    GrantScope,
    MergeAttemptStatus,
    PrincipalType,
    ProposalOrigin,
    ProposalStatus,
)
from lorebase_store.repositories import ConflictRow, MergeAttemptRow, ProposalRow

SCHEMA = Path(__file__).resolve().parents[4] / "examples" / "supply" / "schema.yaml"


def proposal(source: str, target: str, n: int = 1) -> ProposalRow:
    return ProposalRow(
        id=f"prp_{n}",
        title="t",
        description=None,
        source_ref=source,
        target_ref=target,
        origin=ProposalOrigin.MANUAL,
        status=ProposalStatus.OPEN,
        author="usr_u",
        created_at=datetime(2026, 10, 21, tzinfo=UTC),
    )


def guard(
    store: Any,
    scope_type: GrantScope = GrantScope.DOMAIN,
    ref: str | None = None,
    *,
    invisible: bool = True,
) -> ReadGuard:
    store.add_grant("acme", "usr_u", "viewer", scope_type, ref)
    principal = Principal(
        id="usr_u",
        type=PrincipalType.USER,
        display_name="u",
        method="oidc",
        subjects=("usr_u",),
    )
    return ReadGuard(
        Authorizer(AuthzSettings(invisible_kinds=invisible), store=store),
        principal,
        (Permission.DATA_READ,),
        "acme",
        max_depth=100,
    )


async def test_branch_grant_reads_proposals_between_its_branches_only(store: Any) -> None:
    reader = guard(store, GrantScope.BRANCH, "agent/*")
    scope = await conflict_scope(reader, proposal("agent/a", "agent/b"))
    assert scope.everything
    with pytest.raises(ProblemError) as raised:
        await conflict_scope(reader, proposal("agent/a", "main"))
    assert raised.value.status_code == 403


async def test_kind_grant_sees_conflicts_of_its_kinds_only(store: Any) -> None:
    scope = await conflict_scope(
        guard(store, GrantScope.KIND, "Supplier"), proposal("agent/a", "main")
    )
    assert scope == ConflictScope(kinds=frozenset({"Supplier"}), schema=False, invisible=True)

    schema = load_schema(SCHEMA.read_text())
    kinds = {kind.api_name: kind.id for kind in schema.kinds.values()}
    relations = {relation.api_name: relation for relation in schema.relations.values()}
    limited = visible_partitions(ConflictScope(kinds=frozenset({"Supplier", "Part"})), [schema])
    assert limited.allows(kind_partition(kinds["Supplier"]))
    assert limited.allows(kind_partition(kinds["Part"]))
    assert not limited.allows(kind_partition(kinds["Plant"]))
    assert limited.allows(PARTITION_SCHEMA)
    between = [
        r
        for r in relations.values()
        if {r.from_kind, r.to_kind} <= {kinds["Supplier"], kinds["Part"]}
    ]
    assert between
    assert all(limited.allows(relation_partition(r.id)) for r in between)
    assert not limited.allows(relation_partition(relations["stocked_at"].id))
    data_only = visible_partitions(ConflictScope(schema=False), [schema])
    assert not data_only.allows(PARTITION_SCHEMA)
    assert data_only.allows(kind_partition(kinds["Plant"]))
    assert visible_partitions(ConflictScope(), [schema]).everything


async def test_listing_keeps_proposals_the_caller_may_read(store: Any) -> None:
    rows = [proposal("agent/a", "agent/b", 1), proposal("agent/a", "main", 2)]
    narrowed = await readable_proposals(guard(store, GrantScope.BRANCH, "agent/*"), rows)
    assert [r.id for r in narrowed] == ["prp_1"]
    assert len(await readable_proposals(guard(store), rows)) == 2


async def test_listing_with_kind_grants_keeps_every_proposal(store: Any) -> None:
    rows = [proposal("agent/a", "agent/b", 1), proposal("agent/a", "main", 2)]
    assert len(await readable_proposals(guard(store, GrantScope.KIND, "Supplier"), rows)) == 2


def conflict(partition: str) -> ConflictRow:
    return ConflictRow(
        fingerprint="f" * 64,
        code="DV1",
        partition=partition,
        element_key=None,
        attribute_id=None,
        payload={},
        member_count=0,
    )


@pytest.mark.parametrize(("invisible", "status"), [(False, 403), (True, 404)])
async def test_kind_narrowed_caller_cannot_resolve_a_conflict_of_another_kind(
    store: Any, invisible: bool, status: int
) -> None:
    scope = await conflict_scope(
        guard(store, GrantScope.KIND, "Supplier", invisible=invisible),
        proposal("agent/a", "main"),
    )
    schema = load_schema(SCHEMA.read_text())
    kinds = {kind.api_name: kind.id for kind in schema.kinds.values()}
    partitions = visible_partitions(scope, [schema])
    require_visible(partitions, conflict(kind_partition(kinds["Supplier"])), "mat_1")
    for hidden in (kind_partition(kinds["Plant"]), PARTITION_SCHEMA):
        with pytest.raises(ProblemError) as raised:
            require_visible(partitions, conflict(hidden), "mat_1", invisible=scope.invisible)
        assert raised.value.status_code == status
        if invisible:
            # The answer to a Conflict that is not part of the attempt, word for word.
            assert raised.value.problem_detail == unknown_conflict("f" * 64, "mat_1").problem_detail
            assert "Plant" not in raised.value.problem_detail
    require_visible(
        visible_partitions(ConflictScope(), [schema]), conflict(PARTITION_SCHEMA), "mat_1"
    )


async def test_creating_a_proposal_needs_read_on_its_target(store: Any) -> None:
    narrowed = guard(store, GrantScope.BRANCH, "agent/*")
    await require_target_read(narrowed, "agent/b")
    with pytest.raises(ProblemError) as raised:
        await require_target_read(narrowed, "main")
    assert raised.value.status_code == 403


async def test_kind_grants_may_target_any_branch(store: Any) -> None:
    await require_target_read(guard(store, GrantScope.KIND, "Supplier"), "main")


async def test_kind_narrowed_detail_counts_only_visible_conflicts(store: Any) -> None:
    scope = await conflict_scope(
        guard(store, GrantScope.KIND, "Supplier"), proposal("agent/a", "main")
    )
    schema = load_schema(SCHEMA.read_text())
    kinds = {kind.api_name: kind.id for kind in schema.kinds.values()}
    supplier, plant = kind_partition(kinds["Supplier"]), kind_partition(kinds["Plant"])
    partitions = visible_partitions(scope, [schema])
    attempt = MergeAttemptRow(
        id="mat_1",
        proposal_id="prp_1",
        base=None,
        ours=b"o",
        theirs=b"t",
        status=MergeAttemptStatus.CONFLICTED,
        created_at=datetime(2026, 10, 21, tzinfo=UTC),
        outcome={
            "open": 7,
            "resolved": 3,
            "auto_resolutions": [{"partition": supplier}, {"partition": plant}],
            "unmatched_resolutions": ["a" * 64],
        },
    )
    summary = limited_summary(attempt, partitions, {("DV1", False): 2, ("DV1", True): 1})
    assert summary.outcome is not None
    assert (summary.outcome["open"], summary.outcome["resolved"]) == (2, 1)
    assert summary.outcome["auto_resolutions"] == [{"partition": supplier}]
    assert summary.outcome["unmatched_resolutions"] == []


async def test_callers_who_cannot_read_a_proposal_see_no_conflict(store: Any) -> None:
    narrowed = guard(store, GrantScope.BRANCH, "agent/*")
    assert await visible_scope(narrowed, proposal("agent/a", "main")) == NO_SCOPE
    assert (await visible_scope(narrowed, proposal("agent/a", "agent/b"))).everything
    schema = load_schema(SCHEMA.read_text())
    assert visible_partitions(NO_SCOPE, [schema]).include == frozenset()


async def test_conflicts_about_references_to_invisible_kinds_are_left_out(store: Any) -> None:
    scope = await conflict_scope(
        guard(store, GrantScope.KIND, "PurchaseOrder"), proposal("agent/a", "main")
    )
    schema = load_schema(SCHEMA.read_text())
    order = schema.kind_by_api_name("PurchaseOrder")
    assert order is not None
    supplier = order.attribute_by_api_name("supplier")
    status = order.attribute_by_api_name("status")
    assert supplier is not None
    assert status is not None
    partitions = visible_partitions(scope, [schema])
    partition = kind_partition(order.id)
    assert partitions.allows(partition, status.id)
    assert partitions.allows(partition)  # Record-level Conflicts: values are scrubbed
    assert not partitions.allows(partition, supplier.id)
    hidden = replace(conflict(partition), attribute_id=supplier.id)
    with pytest.raises(ProblemError) as raised:
        require_visible(partitions, hidden, "mat_1", invisible=True)
    assert raised.value.status_code == 404


async def test_legacy_scopes_keep_every_attribute_of_a_readable_kind(store: Any) -> None:
    legacy = await conflict_scope(
        guard(store, GrantScope.KIND, "PurchaseOrder", invisible=False),
        proposal("agent/a", "main"),
    )
    schema = load_schema(SCHEMA.read_text())
    order = schema.kind_by_api_name("PurchaseOrder")
    assert order is not None
    supplier = order.attribute_by_api_name("supplier")
    assert supplier is not None
    assert visible_partitions(legacy, [schema]).allows(kind_partition(order.id), supplier.id)


def test_record_level_conflicts_lose_values_and_messages_left_out() -> None:
    schema = load_schema(SCHEMA.read_text())
    order = schema.kind_by_api_name("PurchaseOrder")
    assert order is not None
    supplier = order.attribute_by_api_name("supplier")
    status = order.attribute_by_api_name("status")
    assert supplier is not None
    assert status is not None
    seen = ConflictVisibility(None, UNCLEARED, frozenset({supplier.id}))
    payload = {
        "ours": {supplier.id: "S-1001", status.id: "open"},
        "theirs": {"values": {supplier.id: "S-1002"}},
        "message": f"{supplier.id} differs",
        "details": {"attribute": supplier.id},
    }
    scrubbed = seen.scrub(payload)
    assert scrubbed["ours"] == {status.id: "open"}
    assert scrubbed["theirs"] == {"values": {}}
    assert scrubbed["details"] == {}
    assert supplier.id not in scrubbed["message"]
    assert ConflictVisibility(None, UNCLEARED).scrub(payload) == payload
