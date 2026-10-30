"""Resolution misses, vocabulary suggestions and coverage statistics (design 8.10, 8.11).

- `resolve_misses(domain_id, branch, phrase, misses, callers, first_seen, last_seen)`: the
  phrases `POST /resolve` matched to nothing, counted per Domain and branch, with digests of
  the callers who asked (bounded, never names). Written off the request path by Keeper when
  `store.resolve_misses.enabled`; pruned on `last_seen` and consolidated by Scribe. Indexed
  on `(domain_id, last_seen)` for pruning and `(domain_id, branch, misses)` for consolidation.
- `schema_suggestions` gains the vocabulary types (`kind_alias`, `attribute_alias`,
  `value_alias`, `attribute_description`) and `payload` (the stored value an alias stands for,
  the proposed description). Existing rows keep `{}`.
- `mat_coverage(domain_id, branch, kind_id, revision, computed_at, records, sampled, missing,
  unseen)`: per Kind of a branch head, the Records and how many miss each Attribute's value,
  and per Source how many of its Records no run has seen for its `stale_after`, as counted at
  `revision`. Rows go with their branch.

Every table is Domain-scoped with row-level security like those of revision 0001. Additive:
no data is rewritten.

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-02 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)
_JSON = postgresql.JSONB(astext_type=sa.Text())
OLD_TYPES = "suggestion_type IN ('kind', 'attribute', 'relation')"
NEW_TYPES = (
    "suggestion_type IN ('kind', 'attribute', 'relation', 'kind_alias', 'attribute_alias', "
    "'value_alias', 'attribute_description')"
)
TYPE_CHECK = "ck_schema_suggestions_suggestion_type"


def _isolate(name: str) -> None:
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {name} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def _misses() -> None:
    op.create_table(
        "resolve_misses",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("phrase", sa.Text(), nullable=False),
        sa.Column("misses", sa.BigInteger(), nullable=False),
        sa.Column(
            "callers",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_resolve_misses_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "branch", "phrase", name=op.f("pk_resolve_misses")),
    )
    op.create_index(
        op.f("ix_resolve_misses_domain_id_last_seen"),
        "resolve_misses",
        ["domain_id", "last_seen"],
    )
    op.create_index(
        op.f("ix_resolve_misses_domain_id_branch_misses"),
        "resolve_misses",
        ["domain_id", "branch", "misses"],
    )
    _isolate("resolve_misses")


def _coverage() -> None:
    op.create_table(
        "mat_coverage",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.LargeBinary(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("records", sa.BigInteger(), nullable=False),
        sa.Column("sampled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("missing", _JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("unseen", _JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint(
            "octet_length(revision) = 32", name=op.f("ck_mat_coverage_revision_h32")
        ),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_coverage_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "branch", "kind_id", name=op.f("pk_mat_coverage")),
    )
    _isolate("mat_coverage")


def upgrade() -> None:
    _misses()
    _coverage()
    op.add_column(
        "schema_suggestions",
        sa.Column("payload", _JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.drop_constraint(op.f(TYPE_CHECK), "schema_suggestions", type_="check")
    op.create_check_constraint(op.f(TYPE_CHECK), "schema_suggestions", NEW_TYPES)


def downgrade() -> None:
    op.execute(
        "DELETE FROM schema_suggestions WHERE suggestion_type NOT IN "
        "('kind', 'attribute', 'relation')"
    )
    op.drop_constraint(op.f(TYPE_CHECK), "schema_suggestions", type_="check")
    op.create_check_constraint(op.f(TYPE_CHECK), "schema_suggestions", OLD_TYPES)
    op.drop_column("schema_suggestions", "payload")
    op.drop_table("mat_coverage")
    op.drop_index(op.f("ix_resolve_misses_domain_id_branch_misses"), table_name="resolve_misses")
    op.drop_index(op.f("ix_resolve_misses_domain_id_last_seen"), table_name="resolve_misses")
    op.drop_table("resolve_misses")
