"""Agent trace steps (`keeper.trace`): how an agent session navigated the graph.

Debugging data, not compliance: rows are written best effort and pruned after the retention
period. Never values of Records: the query as the caller sent it (filter literals redacted
unless configured otherwise), result counts and the refs (Kind and key) a response touched.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Double, Identity, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import Base

STEP_TYPES = ("call", "note")


class AgentTraceStep(Base):
    """One traced request (`call`) or one note an agent attached to its session (`note`).

    `domain_id` is null for notes and for calls outside a Domain; it has no foreign key, so
    steps outlive a deleted Domain until the retention period ends.
    """

    __tablename__ = "agent_trace_steps"
    __table_args__ = (
        CheckConstraint("type IN ('call', 'note')", name="type"),
        Index(None, "actor", "session_id", "seq"),
        Index(None, "session_id", "seq"),
        Index(None, "domain_id", "at"),
        Index(None, "at"),
        Index(None, "actor", "fingerprint", "at", postgresql_where=text("inferred")),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    session_id: Mapped[str]
    actor: Mapped[str]
    principal_type: Mapped[str | None]
    domain_id: Mapped[str | None]
    at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    type: Mapped[str]
    tool: Mapped[str | None]
    method: Mapped[str | None]
    route: Mapped[str | None]
    status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Double)
    cache: Mapped[str | None]
    branch: Mapped[str | None]
    query: Mapped[dict[str, Any] | None]
    result: Mapped[dict[str, Any] | None]
    refs: Mapped[dict[str, Any] | None]
    explain: Mapped[dict[str, Any] | None]
    note: Mapped[str | None]
    proposal_id: Mapped[str | None]
    request_id: Mapped[str | None]
    trace_id: Mapped[str | None]
    inferred: Mapped[bool] = mapped_column(server_default=text("false"))
    """The session was inferred (no session header): grouped by principal and client
    fingerprint while requests keep coming within `keeper.trace.inferred_session_idle_s`."""
    fingerprint: Mapped[str | None]
    """Digest of the principal, its credential and user agent; set on inferred steps."""
