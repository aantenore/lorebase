"""Outbox events, their consumers and the job queue (design 7.4, 10.5).

Neither events nor jobs reference `domains`: events and jobs about a Domain outlive its
deletion.

Each consumer of the outbox keeps its own delivery state: a trigger on `outbox` (revision 0020)
adds one `outbox_pending` row per registered consumer in the transaction that writes the event,
and the consumer deletes its row once the event is handed off. An event is pruned only when no
consumer still has it pending (or when it is older than `store.outbox.max_age_s`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKeyConstraint, Identity, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import Base
from lorebase_store.models.enums import JOB_STATUS, JobStatus


class OutboxEvent(Base):
    """Event written in the commit transaction and delivered later by Scribe."""

    __tablename__ = "outbox"
    __table_args__ = (Index(None, "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    domain_id: Mapped[str]
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class OutboxConsumer(Base):
    """A consumer of the outbox (the webhook worker is `webhooks`); events written after it
    is registered are pending for it until it hands them off."""

    __tablename__ = "outbox_consumers"

    name: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class OutboxPending(Base):
    """An event one consumer has not handed off yet (its delivery state)."""

    __tablename__ = "outbox_pending"
    __table_args__ = (
        ForeignKeyConstraint(["consumer"], ["outbox_consumers.name"], ondelete="CASCADE"),
        ForeignKeyConstraint(["event_id"], ["outbox.id"], ondelete="CASCADE"),
        Index(None, "event_id"),
    )

    consumer: Mapped[str] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    domain_id: Mapped[str]


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
