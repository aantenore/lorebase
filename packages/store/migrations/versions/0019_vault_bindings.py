"""Vault rows name the value they hold (design 13.5).

The `vault` table of revision 0001 had no writer. A row now binds its token to the Kind,
Record key and Attribute whose value it holds, and keeps a keyed fingerprint of the value for
erasure by value (`lorebase_store.vault`). Rows are found by Record (`domain_id, kind_id,
record_key`) and by value (`domain_id, kind_id, attribute_id, fingerprint`). The new columns
are required; the table must be empty, as nothing wrote to it before this revision.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-24 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("kind_id", "attribute_id", "record_key"):
        op.add_column("vault", sa.Column(column, sa.Text(), nullable=False))
    op.add_column("vault", sa.Column("fingerprint", sa.LargeBinary(), nullable=True))
    op.create_index(
        op.f("ix_vault_domain_id_kind_id_record_key"),
        "vault",
        ["domain_id", "kind_id", "record_key"],
    )
    op.create_index(
        op.f("ix_vault_domain_id_kind_id_attribute_id_fingerprint"),
        "vault",
        ["domain_id", "kind_id", "attribute_id", "fingerprint"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Row-level security is forced on the owner too: count the rows of every Domain.
    bind.execute(sa.text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
    stored = bind.execute(sa.text("SELECT count(*) FROM vault")).scalar_one()
    if stored:
        raise RuntimeError(
            f"{stored} vault row(s) exist, which revision 0018 cannot bind to their Records; "
            "erase or delete them before downgrading"
        )
    op.drop_index(op.f("ix_vault_domain_id_kind_id_attribute_id_fingerprint"), table_name="vault")
    op.drop_index(op.f("ix_vault_domain_id_kind_id_record_key"), table_name="vault")
    for column in ("fingerprint", "record_key", "attribute_id", "kind_id"):
        op.drop_column("vault", column)
