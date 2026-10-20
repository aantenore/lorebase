"""Outbox events and the job queue (design 7.4, 10.5).

Neither table references `domains`: events and jobs about a Domain outlive its deletion.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import Base
from lorebase_store.models.enums import JOB_STATUS, JobStatus


class OutboxEvent(Base):
    """Event written in the commit transaction and delivered later by Scribe."""

    __tablename__ = "outbox"
    __table_args__ = (
        Index(
            "ix_outbox_undelivered",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    domain_id: Mapped[str]
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    delivered_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class Job(Base):
    """Background job; workers claim `queued` rows with `FOR UPDATE SKIP LOCKED`."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_claimable",
            text("priority DESC"),
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(None, "domain_id", "type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    type: Mapped[str]
    domain_id: Mapped[str | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(JOB_STATUS, server_default=text("'queued'"))
    priority: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(server_default=text("now()"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_by: Mapped[str | None]
    locked_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
