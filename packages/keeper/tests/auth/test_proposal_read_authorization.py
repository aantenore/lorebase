"""Proposal reads are authorized against both branches, and Kind grants limit the Conflicts."""

from __future__ import annotations

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
from lorebase_keeper.services.proposal_reads import (
    ConflictScope,
    conflict_scope,
    readable_proposals,
    visible_partitions,
)
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.models.enums import GrantScope, PrincipalType, ProposalOrigin, ProposalStatus
from lorebase_store.repositories import ProposalRow

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
    store: Any, scope_type: GrantScope = GrantScope.DOMAIN, ref: str | None = None
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
        Authorizer(AuthzSettings(), store=store),
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
    assert scope == ConflictScope(kinds=frozenset({"Supplier"}), schema=False)

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
