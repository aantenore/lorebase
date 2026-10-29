"""Proposal review policy and routes without a database."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from lorebase_keeper.errors import ProblemError
from lorebase_keeper.routes.proposals import _checks
from lorebase_keeper.services.config import ProposalSettings
from lorebase_keeper.services.proposals import (
    RoleApproval,
    approval_of,
    domain_policy,
    proposal_policy,
    required_approvals,
    required_role_names,
    role_approval,
)
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus, ReviewVerdict
from lorebase_store.repositories import DomainRow, ProposalRow, ReviewRow

T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
V = ReviewVerdict


def domain(**settings: object) -> DomainRow:
    return DomainRow(
        id="supply",
        display_name="Supply",
        description=None,
        default_branch="main",
        leaf_size=512,
        settings=dict(settings),
        created_at=T0,
        created_by="usr_admin",
    )


def proposal(**policies: object) -> ProposalRow:
    return ProposalRow(
        id="prp_1",
        title="t",
        description=None,
        source_ref="work",
        target_ref="main",
        origin=ProposalOrigin.MANUAL,
        status=ProposalStatus.OPEN,
        author="usr_author",
        created_at=T0,
        policies=dict(policies),
    )


def reviews(*verdicts: tuple[str, ReviewVerdict]) -> list[ReviewRow]:
    return [
        ReviewRow(
            id=f"prr_{n}",
            proposal_id="prp_1",
            reviewer=reviewer,
            verdict=verdict,
            comment=None,
            at=T0 + timedelta(minutes=n),
        )
        for n, (reviewer, verdict) in enumerate(verdicts)
    ]


def test_latest_decisive_verdict_per_reviewer_counts() -> None:
    state = approval_of(
        proposal(),
        reviews(
            ("usr_a", V.REQUEST_CHANGES),
            ("usr_a", V.APPROVE),
            ("usr_a", V.COMMENT),
            ("usr_b", V.APPROVE),
            ("usr_c", V.REQUEST_CHANGES),
        ),
        protected=True,
        required=2,
        settings=ProposalSettings(),
    )
    assert state.approvers == ("usr_a", "usr_b")
    assert state.changes_requested == ("usr_c",)
    assert not state.satisfied
    lenient = ProposalSettings(changes_requested_blocks=False)
    later = approval_of(
        proposal(),
        reviews(("usr_a", V.APPROVE), ("usr_c", V.REQUEST_CHANGES)),
        protected=True,
        required=1,
        settings=lenient,
    )
    assert later.satisfied


def test_author_approval_counts_only_when_allowed() -> None:
    own = reviews(("usr_author", V.APPROVE))
    strict = approval_of(proposal(), own, protected=True, required=1, settings=ProposalSettings())
    assert not strict.satisfied
    allowed = ProposalSettings(allow_self_approval=True)
    assert approval_of(proposal(), own, protected=True, required=1, settings=allowed).satisfied


def test_required_roles_need_an_approval_from_each_role() -> None:
    base = approval_of(
        proposal(),
        reviews(("usr_a", V.APPROVE)),
        protected=True,
        required=1,
        settings=ProposalSettings(),
    )
    assert base.satisfied
    both = RoleApproval(("a", "b"), {"a": ("usr_a",), "b": ()})
    assert both.missing == ("b",)
    assert not replace(base, roles=both).satisfied
    done = RoleApproval(("a", "b"), {"a": ("usr_a",), "b": ("usr_a",)})
    assert replace(base, roles=done).satisfied
    assert not replace(base, roles=RoleApproval(("a",), {})).satisfied  # fail closed
    unprotected = replace(base, protected=False, roles=both)
    assert unprotected.satisfied


def test_required_roles_setting_and_domain_override() -> None:
    settings = ProposalSettings(required_roles=["x", "x", "y"])
    assert required_role_names(domain(), settings) == ("x", "y")
    assert required_role_names(domain(required_roles=["z"]), settings) == ("z",)
    assert required_role_names(domain(required_roles=[]), settings) == ()
    assert required_role_names(domain(required_roles="z"), settings) == ("x", "y")
    assert required_role_names(domain(required_roles=["z", 3]), settings) == ("x", "y")


async def test_role_rule_off_unprotected_or_without_stewards() -> None:
    off = approval_of(proposal(), [], protected=True, required=1, settings=ProposalSettings())
    on = ProposalSettings(required_roles=["x"])
    assert (
        await role_approval(None, domain(), proposal(), off, ProposalSettings(), None, None) is None
    )  # type: ignore[arg-type]
    open_ = replace(off, protected=False)
    assert await role_approval(None, domain(), proposal(), open_, on, None, None) is None  # type: ignore[arg-type]
    closed = await role_approval(None, domain(), proposal(), off, on, None, None)  # type: ignore[arg-type]
    assert closed is not None
    assert closed.missing == ("x",)


def test_unprotected_targets_need_no_approval() -> None:
    state = approval_of(proposal(), [], protected=False, required=3, settings=ProposalSettings())
    assert state.satisfied


def test_domain_settings_override_approvals_and_policy() -> None:
    settings = ProposalSettings(required_approvals=2)
    assert required_approvals(domain(), settings) == 2
    assert required_approvals(domain(required_approvals=0), settings) == 0
    assert required_approvals(domain(required_approvals="x"), settings) == 2
    policy = domain_policy(domain(merge_policy={"prefer": "theirs", "fast_forward": False}))
    assert policy.prefer is not None
    assert policy.prefer.value == "theirs"
    assert not policy.fast_forward
    stored = proposal(merge_policy=policy.model_dump(mode="json", exclude_defaults=True))
    assert proposal_policy(stored) == policy
    with pytest.raises(ProblemError) as raised:
        domain_policy(domain(merge_policy={"prefer": "base"}))
    assert raised.value.status_code == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/domains/supply/proposals"),
        ("POST", "/api/v1/domains/supply/proposals/prp_1/merge"),
        ("GET", "/api/v1/domains/supply/proposals/prp_1/conflicts"),
    ],
)
async def test_proposal_routes_need_a_database(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"


def test_merge_check_states_no_count_for_partial_readers() -> None:
    row = replace(proposal(), checks={"merge": {"open_conflicts": 4, "ours": "ab" * 32}})
    assert _checks(row)[0].message == f"4 open Conflict(s) at {'ab' * 6}."
    hidden = _checks(row, counted=False)[0]
    assert hidden.status == "failed"
    assert hidden.message is not None
    assert "4" not in hidden.message
