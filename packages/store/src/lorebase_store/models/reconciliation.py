"""Reconciliation candidates (design 10.6): contradictions between Sources awaiting a decision."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import H32, Base, DomainKey, domain_fk, h32_check


class ReconciliationCandidate(Base):
    """Claims that disagree about one value (or two Records that look alike), keyed by the
    fingerprint of their values on a branch.

    `claims` holds the newest claims (values of sensitive Attributes wrapped with their tags,
    as in outbox payloads), `claim_count` every distinct claim seen; `decision` the last
    decision with the claims as they were then."""

    __tablename__ = "reconciliation_candidates"
    __table_args__ = (
        domain_fk(),
        h32_check("fingerprint"),
        CheckConstraint(
            "type IN ('duplicate', 'contradiction', 'superseded', 'stale')", name="type"
        ),
        CheckConstraint("status IN ('open', 'resolved', 'dismissed', 'obsolete')", name="status"),
        UniqueConstraint("domain_id", "branch", "fingerprint"),
        Index(None, "domain_id", "branch", "status", "type"),
        Index(None, "domain_id", "branch", "kind_id", "keys"),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    branch: Mapped[str]
    type: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))
    fingerprint: Mapped[H32]
    kind_id: Mapped[str]
    keys: Mapped[list[str]] = mapped_column(JSONB)
    attribute_id: Mapped[str | None]
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    signals: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    claim_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    proposal_id: Mapped[str | None]
    decision: Mapped[dict[str, Any] | None]
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
