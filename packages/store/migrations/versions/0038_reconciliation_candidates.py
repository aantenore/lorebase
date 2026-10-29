"""Reconciliation candidates and validity periods on provenance (design 10.6).

`provenance.valid_from` and `valid_to` hold the validity period of the values of a `valid_time`
Attribute; existing entries read as "no period" (metadata-only change, no backfill).
`reconciliation_candidates` holds contradictions between Sources awaiting a decision, one per
`(domain_id, branch, fingerprint)`. Under the `domain_isolation` policy like every table with
a `domain_id`.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-29 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CANDIDATES = "reconciliation_candidates"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def upgrade() -> None:
    for column in ("valid_from", "valid_to"):
        op.add_column("provenance", sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        CANDIDATES,
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attribute_id", sa.Text(), nullable=True),
        sa.Column(
            "claims",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "signals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("claim_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(fingerprint) = 32",
            name=op.f("ck_reconciliation_candidates_fingerprint_h32"),
        ),
        sa.CheckConstraint(
            "type IN ('duplicate', 'contradiction', 'superseded', 'stale')",
            name=op.f("ck_reconciliation_candidates_type"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed', 'obsolete')",
            name=op.f("ck_reconciliation_candidates_status"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_reconciliation_candidates_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_reconciliation_candidates")),
        sa.UniqueConstraint(
            "domain_id",
            "branch",
            "fingerprint",
            name=op.f("uq_reconciliation_candidates_domain_id_branch_fingerprint"),
        ),
    )
    op.create_index(
        op.f("ix_reconciliation_candidates_domain_id_branch_status_type"),
        CANDIDATES,
        ["domain_id", "branch", "status", "type"],
    )
    op.create_index(
        op.f("ix_reconciliation_candidates_domain_id_branch_kind_id_keys"),
        CANDIDATES,
        ["domain_id", "branch", "kind_id", "keys"],
    )
    op.execute(f"ALTER TABLE {CANDIDATES} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {CANDIDATES} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {CANDIDATES} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_reconciliation_candidates_domain_id_branch_kind_id_keys"), table_name=CANDIDATES
    )
    op.drop_index(
        op.f("ix_reconciliation_candidates_domain_id_branch_status_type"), table_name=CANDIDATES
    )
    op.drop_table(CANDIDATES)
    for column in ("valid_to", "valid_from"):
        op.drop_column("provenance", column)
