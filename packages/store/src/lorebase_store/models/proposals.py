"""Proposals and merges (design 7.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import REAL, ForeignKeyConstraint, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import H32, Base, DomainKey, domain_fk
from lorebase_store.models.enums import (
    ITEM_DECISION,
    MERGE_ATTEMPT_STATUS,
    PROPOSAL_ORIGIN,
    PROPOSAL_STATUS,
    REVIEW_VERDICT,
    ItemDecision,
    MergeAttemptStatus,
    ProposalOrigin,
    ProposalStatus,
    ReviewVerdict,
)


def _proposal_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["domain_id", "proposal_id"],
        ["proposals.domain_id", "proposals.id"],
        ondelete="CASCADE",
    )


class Proposal(Base):
    """Request to merge a source ref into a target ref."""

    __tablename__ = "proposals"
    __table_args__ = (
        domain_fk(),
        Index(None, "domain_id", "target_ref", "status"),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    description: Mapped[str | None]
    source_ref: Mapped[str]
    target_ref: Mapped[str]
    origin: Mapped[ProposalOrigin] = mapped_column(PROPOSAL_ORIGIN)
    status: Mapped[ProposalStatus] = mapped_column(PROPOSAL_STATUS)
    author: Mapped[str]
    created_at: Mapped[datetime]
    merged_revision: Mapped[H32 | None]
    checks: Mapped[dict[str, Any] | None]
    policies: Mapped[dict[str, Any] | None]


class ProposalReview(Base):
    """One review of a Proposal; a reviewer may review several times."""

    __tablename__ = "proposal_reviews"
    __table_args__ = (_proposal_fk(), Index(None, "domain_id", "proposal_id"))

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    proposal_id: Mapped[str]
    reviewer: Mapped[str]
    verdict: Mapped[ReviewVerdict] = mapped_column(REVIEW_VERDICT)
    comment: Mapped[str | None]
    at: Mapped[datetime]


class ProposalItem(Base):
    """Per-item decision for extraction Proposals."""

    __tablename__ = "proposal_items"
    __table_args__ = (_proposal_fk(),)

    domain_id: Mapped[DomainKey]
    proposal_id: Mapped[str] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(primary_key=True)
    partition: Mapped[str]
    element_key: Mapped[str]
    attribute_id: Mapped[str | None]
    evidence: Mapped[dict[str, Any] | None]
    confidence: Mapped[float | None] = mapped_column(REAL)
    decision: Mapped[ItemDecision] = mapped_column(ITEM_DECISION)
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None]


class MergeAttempt(Base):
    """Stored three-way merge with Conflicts, attached to a Proposal."""

    __tablename__ = "merge_attempts"
    __table_args__ = (_proposal_fk(), Index(None, "domain_id", "proposal_id"))

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    proposal_id: Mapped[str]
    base: Mapped[H32]
    ours: Mapped[H32]
    theirs: Mapped[H32]
    status: Mapped[MergeAttemptStatus] = mapped_column(MERGE_ATTEMPT_STATUS)
    created_at: Mapped[datetime]


class Conflict(Base):
    """One Conflict of a merge attempt, identified by its fingerprint (design 6.10)."""

    __tablename__ = "conflicts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["domain_id", "attempt_id"],
            ["merge_attempts.domain_id", "merge_attempts.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("domain_id", "attempt_id", "fingerprint"),
    )

    domain_id: Mapped[DomainKey]
    attempt_id: Mapped[str] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(primary_key=True)
    code: Mapped[str]
    fingerprint: Mapped[H32]
    partition: Mapped[str]
    element_key: Mapped[str | None]
    attribute_id: Mapped[str | None]
    payload: Mapped[dict[str, Any]]
    member_count: Mapped[int] = mapped_column(Integer)
    resolution: Mapped[dict[str, Any] | None]
    resolved_by: Mapped[str | None]
    resolved_at: Mapped[datetime | None]


class ConflictResolution(Base):
    """Resolution stored by fingerprint so it survives merge attempt recomputation."""

    __tablename__ = "conflict_resolutions"
    __table_args__ = (_proposal_fk(),)

    domain_id: Mapped[DomainKey]
    proposal_id: Mapped[str] = mapped_column(primary_key=True)
    fingerprint: Mapped[H32] = mapped_column(primary_key=True)
    resolution: Mapped[dict[str, Any]]
    resolved_by: Mapped[str]
    at: Mapped[datetime]
