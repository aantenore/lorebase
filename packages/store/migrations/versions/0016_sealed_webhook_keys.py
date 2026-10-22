"""Webhook signing keys may be stored sealed.

`webhooks.sealed_key` holds the signing key (`sha256(secret)`) encrypted under
`webhooks.encryption_key` (AES-256-GCM, `lorebase_store.webhooks.keys`); `secret_hash`, which
held it usable, becomes nullable and exactly one of the two is set. Existing rows keep their
`secret_hash` and keep working: the encryption key lives in the services' configuration, not
here, so the delivery worker seals them once a key is configured.

The downgrade refuses to run while a row is sealed: only the services can open it. Unset
`webhooks.encryption_key` is not enough; delete those webhooks (or downgrade a copy without
them) first.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("webhooks", sa.Column("sealed_key", sa.LargeBinary(), nullable=True))
    op.alter_column("webhooks", "secret_hash", existing_type=sa.LargeBinary(), nullable=True)
    op.create_check_constraint(
        op.f("ck_webhooks_one_signing_key"),
        "webhooks",
        "(secret_hash IS NULL) <> (sealed_key IS NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Row-level security is forced on the owner too: count the rows of every Domain.
    bind.execute(sa.text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
    sealed = bind.execute(
        sa.text("SELECT count(*) FROM webhooks WHERE sealed_key IS NOT NULL")
    ).scalar_one()
    if sealed:
        raise RuntimeError(
            f"{sealed} webhook(s) have a sealed signing key, which revision 0015 cannot hold; "
            "delete them before downgrading"
        )
    op.drop_constraint(op.f("ck_webhooks_one_signing_key"), "webhooks", type_="check")
    op.alter_column("webhooks", "secret_hash", existing_type=sa.LargeBinary(), nullable=False)
    op.drop_column("webhooks", "sealed_key")
