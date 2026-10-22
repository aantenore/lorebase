"""Principals, groups, grants, API keys, web sessions, Operation invocations and audit
(design 7.4, 10, 13)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import H32, Base, DomainKey, domain_fk, h32_check
from lorebase_store.models.enums import (
    GRANT_SCOPE,
    INVOCATION_STATUS,
    PRINCIPAL_TYPE,
    GrantScope,
    InvocationStatus,
    PrincipalType,
)


class Principal(Base):
    """User, service or API key identity. Ids are prefixed (`usr_`, `svc_`)."""

    __tablename__ = "principals"
    __table_args__ = (
        Index(
            None,
            "external_subject",
            unique=True,
            postgresql_where=text("external_subject IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    type: Mapped[PrincipalType] = mapped_column(PRINCIPAL_TYPE)
    display_name: Mapped[str]
    external_subject: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Group(Base):
    """Group of principals, optionally mapped from an identity provider claim."""

    __tablename__ = "groups"
    __table_args__ = (
        Index(
            None,
            "external_group",
            unique=True,
            postgresql_where=text("external_group IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    display_name: Mapped[str]
    external_group: Mapped[str | None]


class GroupMember(Base):
    """Membership of a principal in a group."""

    __tablename__ = "group_members"
    __table_args__ = (Index(None, "principal_id"),)

    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )


class Grant(Base):
    """Role of a subject (principal or group id) in a Domain, optionally scoped."""

    __tablename__ = "grants"
    __table_args__ = (domain_fk(), Index(None, "domain_id", "subject"), Index(None, "subject"))

    id: Mapped[str] = mapped_column(primary_key=True)
    domain_id: Mapped[str]
    subject: Mapped[str]
    role: Mapped[str]
    scope_type: Mapped[GrantScope] = mapped_column(GRANT_SCOPE)
    scope_ref: Mapped[str | None]
    created_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class ApiKey(Base):
    """API key of a service principal; only an HMAC of the secret is stored."""

    __tablename__ = "api_keys"
    __table_args__ = (h32_check("hash"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    prefix: Mapped[str] = mapped_column(unique=True)
    hash: Mapped[H32]
    scopes: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    # Client networks the key may be used from; null: any address.
    allowed_networks: Mapped[list[Any] | None] = mapped_column(ARRAY(CIDR))


class ApiKeyUsage(Base):
    """Requests and context-pack tokens a key used on one UTC day (daily quotas)."""

    __tablename__ = "api_key_usage"

    api_key_id: Mapped[str] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    context_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class WebSession(Base):
    """Signed-in browser session of a user (Keeper as backend-for-frontend, design 13.2).

    Only the SHA-256 digest of the session id is stored; `tokens` holds the
    provider's refresh and ID tokens encrypted by Keeper. Installation-wide like `api_keys`.
    """

    __tablename__ = "web_sessions"
    __table_args__ = (h32_check("id_hash"),)

    id_hash: Mapped[H32] = mapped_column(primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    subject: Mapped[str]
    external_groups: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    tokens: Mapped[bytes | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(index=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    refresh_at: Mapped[datetime | None]


class OperationInvocation(Base):
    """One Operation execution; idempotent per principal and key."""

    __tablename__ = "operation_invocations"
    __table_args__ = (
        domain_fk(),
        Index(
            "uq_operation_invocations_idempotency",
            "domain_id",
            "principal",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    operation_id: Mapped[str]
    principal: Mapped[str]
    params: Mapped[dict[str, Any]]
    status: Mapped[InvocationStatus] = mapped_column(INVOCATION_STATUS)
    revision_id: Mapped[H32 | None]
    proposal_id: Mapped[str | None]
    idempotency_key: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class _AuditColumns:
    """Columns of a hash-chained audit table."""

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    domain_id: Mapped[str | None]
    at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    actor: Mapped[str]
    action: Mapped[str]
    target: Mapped[str | None]
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    prev_hash: Mapped[H32 | None]
    entry_hash: Mapped[H32]


class AuditEntry(_AuditColumns, Base):
    """Hash-chained audit entry: `entry_hash = sha256(prev_hash || canonical entry)`.

    `domain_id` is null for installation-wide actions and has no foreign key, so audit
    survives Domain deletion.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        h32_check("entry_hash"),
        Index(None, "domain_id", "seq"),
        Index(None, "entry_hash", unique=True),
    )


class AuditReadEntry(_AuditColumns, Base):
    """Hash-chained record of a read (`keeper.auth.audit.reads`), chained like `audit_log` but
    in chains of its own, so sampled, best-effort read records never touch the chains of
    actions."""

    __tablename__ = "audit_read_log"
    __table_args__ = (
        h32_check("entry_hash"),
        Index(None, "domain_id", "seq"),
        Index(None, "entry_hash", unique=True),
    )


class RateLimitBucket(Base):
    """Shared token bucket of Keeper's rate limits (`keeper.limits.rate.backend: postgres`).

    UNLOGGED: buckets are lost on a crash of the server and start full again. `key` is the
    SHA-256 of the bucket name, so client addresses and principal ids are not stored;
    `full_at` (epoch seconds) is when the bucket will be full, after which the row is
    redundant and may be deleted.
    """

    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        h32_check("key"),
        Index(None, "full_at"),
        {"prefixes": ["UNLOGGED"]},
    )

    key: Mapped[H32] = mapped_column(primary_key=True)
    tokens: Mapped[float] = mapped_column(Double)
    updated: Mapped[float] = mapped_column(Double)
    full_at: Mapped[float] = mapped_column(Double)
    granted: Mapped[float] = mapped_column(Double)
