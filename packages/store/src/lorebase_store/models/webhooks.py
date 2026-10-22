"""Webhooks of a Domain and their deliveries (design 10.5).

A webhook receives the outbox events of its Domain (optionally narrowed to event types and
branches) as HMAC-signed POST requests. Only a SHA-256 digest of its secret is kept; the
digest is the signing key (`lorebase_store.webhooks.signing`), stored sealed in `sealed_key`
when `webhooks.encryption_key` is set, else usable in `secret_hash` (`webhooks.keys`).
Deliveries are fanned out from the outbox and retried with backoff until delivered or dead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import H32, Base, DomainKey, domain_fk, h32_check

DELIVERY_STATUSES = ("pending", "delivered", "dead")


class Webhook(Base):
    """An HTTP endpoint notified of the events of a Domain."""

    __tablename__ = "webhooks"
    __table_args__ = (
        domain_fk(),
        h32_check("secret_hash"),
        CheckConstraint("(secret_hash IS NULL) <> (sealed_key IS NULL)", name="one_signing_key"),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    url: Mapped[str]
    description: Mapped[str | None]
    event_types: Mapped[Any] = mapped_column(JSONB)
    """Event types delivered (a JSON array of strings)."""
    branches: Mapped[Any | None] = mapped_column(JSONB)
    """Branch names whose events are delivered (a JSON array); null for every branch."""
    clearances: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    """Sensitivity tags whose values the webhook receives in clear (a JSON array); values of
    other sensitive Attributes reach it masked."""
    secret_hash: Mapped[H32 | None]
    """The signing key, stored usable (no `webhooks.encryption_key` when it was stored)."""
    sealed_key: Mapped[bytes | None]
    """The signing key sealed under `webhooks.encryption_key` (nonce and AES-GCM ciphertext)."""
    created_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class WebhookDelivery(Base):
    """One outbox event to deliver to one webhook, with its attempts."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["domain_id", "webhook_id"],
            ["webhooks.domain_id", "webhooks.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("domain_id", "webhook_id", "event_id"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in DELIVERY_STATUSES) + ")", name="status"
        ),
        Index(
            "ix_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    webhook_id: Mapped[str]
    event_id: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None]
    delivered_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
