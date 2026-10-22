"""Read audit log and shared rate limit buckets.

`audit_read_log` holds the records of reads (`keeper.auth.audit.reads`) in hash chains of its
own, one per Domain plus an installation-wide one, computed like those of `audit_log`. It is
append-only for every role but its owner, like `audit_log` (revision 0015), and under the
`domain_isolation` policy.

`rate_limit_buckets` holds the token buckets of `keeper.limits.rate.backend: postgres`, shared
by every Keeper. It is UNLOGGED: no WAL, nothing replicated, emptied after a crash of the
server (buckets start full again).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-25 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READS = "audit_read_log"
BUCKETS = "rate_limit_buckets"
REVOKED = "UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def _application_role() -> str | None:
    config = op.get_context().config
    if config is None or "database_settings" not in config.attributes:
        raise RuntimeError("run this migration through the Lorebase Alembic environment")
    role: str | None = config.attributes["database_settings"].application_role
    return role


def _quoted(role: str) -> str:
    quoted: str = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar_one()
    return quoted


def _holders() -> list[str]:
    """Roles other than the owner holding a revoked privilege on the read log (default
    privileges of the owner grant them at creation)."""
    return list(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT DISTINCT quote_ident(r.rolname) FROM pg_class c "
                "CROSS JOIN LATERAL aclexplode(c.relacl) a "
                "JOIN pg_roles r ON r.oid = a.grantee "
                "WHERE c.oid = to_regclass(:t) AND a.grantee <> c.relowner "
                "AND a.privilege_type IN "
                "('UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER') ORDER BY 1"
            ),
            {"t": READS},
        )
        .scalars()
        .all()
    )


def upgrade() -> None:
    op.create_table(
        READS,
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.LargeBinary(), nullable=True),
        sa.Column("entry_hash", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "octet_length(entry_hash) = 32", name=op.f("ck_audit_read_log_entry_hash_h32")
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_audit_read_log")),
    )
    op.create_index(op.f("ix_audit_read_log_domain_id_seq"), READS, ["domain_id", "seq"])
    op.create_index(op.f("ix_audit_read_log_entry_hash"), READS, ["entry_hash"], unique=True)
    op.execute(f"ALTER TABLE {READS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {READS} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {READS} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )
    op.execute(f"REVOKE {REVOKED} ON TABLE {READS} FROM PUBLIC")
    roles = set(_holders())
    role = _application_role()
    if role is not None:
        roles.add(_quoted(role))
    for quoted in sorted(roles):
        op.execute(f"REVOKE {REVOKED} ON TABLE {READS} FROM {quoted}")

    op.create_table(
        BUCKETS,
        sa.Column("key", sa.LargeBinary(), nullable=False),
        sa.Column("tokens", sa.Double(), nullable=False),
        sa.Column("updated", sa.Double(), nullable=False),
        sa.Column("full_at", sa.Double(), nullable=False),
        sa.Column("granted", sa.Double(), nullable=False),
        sa.CheckConstraint("octet_length(key) = 32", name=op.f("ck_rate_limit_buckets_key_h32")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_rate_limit_buckets")),
        prefixes=["UNLOGGED"],
    )
    op.create_index(op.f("ix_rate_limit_buckets_full_at"), BUCKETS, ["full_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_rate_limit_buckets_full_at"), table_name=BUCKETS)
    op.drop_table(BUCKETS)
    op.drop_index(op.f("ix_audit_read_log_entry_hash"), table_name=READS)
    op.drop_index(op.f("ix_audit_read_log_domain_id_seq"), table_name=READS)
    op.drop_table(READS)
