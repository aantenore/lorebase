"""Schema suggestions aggregated from document extraction (design 9.2 step 10).

`schema_suggestions` holds, per Domain, the new Kinds, Attributes and Relations extraction found
outside the schema, one row per `fingerprint`, with support, examples and sample values, and
the reviewer's decision. Domain-scoped with row-level security like every table of 0001.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-25 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)
_JSON = postgresql.JSONB(astext_type=sa.Text())


def _json(name: str, default: str) -> sa.Column[object]:
    return sa.Column(name, _JSON, server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "schema_suggestions",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("suggestion_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind_ref", sa.Text(), nullable=True),
        sa.Column("target_ref", sa.Text(), nullable=True),
        _json("labels", "[]"),
        sa.Column("support", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("confidence", sa.Float(), server_default=sa.text("0"), nullable=False),
        _json("observations", "{}"),
        _json("examples", "[]"),
        _json("samples", "[]"),
        _json("sources", "[]"),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("last_job_id", sa.Text(), nullable=True),
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
            "suggestion_type IN ('kind', 'attribute', 'relation')",
            name=op.f("ck_schema_suggestions_suggestion_type"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name=op.f("ck_schema_suggestions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_schema_suggestions_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_schema_suggestions")),
    )
    op.create_index(
        op.f("ix_schema_suggestions_domain_id_fingerprint"),
        "schema_suggestions",
        ["domain_id", "fingerprint"],
        unique=True,
    )
    op.create_index(
        op.f("ix_schema_suggestions_domain_id_status_support"),
        "schema_suggestions",
        ["domain_id", "status", "support"],
        unique=False,
    )
    op.execute("ALTER TABLE schema_suggestions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE schema_suggestions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY domain_isolation ON schema_suggestions "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_schema_suggestions_domain_id_status_support"), table_name="schema_suggestions"
    )
    op.drop_index(
        op.f("ix_schema_suggestions_domain_id_fingerprint"), table_name="schema_suggestions"
    )
    op.drop_table("schema_suggestions")
