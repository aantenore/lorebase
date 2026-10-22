"""Vault key rotation, erasure tombstones and lineage, installation secrets (design 13.5).

- `vault.fingerprint_key` names the token key and scheme a row's fingerprint was made under
  (`<token_key_id>/<scheme>`); rows stored before have none and are made again by the rotation
  job (`lorebase_store.vault.rotation`).
- `vault_tombstones` keeps the fingerprints of erased values, so a later write of one is stored
  erased or refused (`vault.erased_values`).
- `vault_lineage` records a Record that took over the vaulted values of another key of its Kind
  (a key change), so erasing one erases both.
- `installation_secrets` holds a random secret made here: the key of the keyed digests
  (Operation parameters, erased Record keys in the audit log) when no `vault.digest_key` and no
  vault keys are configured.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-27 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOMBSTONES = "vault_tombstones"
LINEAGE = "vault_lineage"
SECRETS = "installation_secrets"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def _isolate(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {table} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def upgrade() -> None:
    op.add_column("vault", sa.Column("fingerprint_key", sa.Text(), nullable=True))

    op.create_table(
        TOMBSTONES,
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("attribute_id", sa.Text(), nullable=False),
        sa.Column("record_key", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("fingerprint_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_vault_tombstones_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "seq", name=op.f("pk_vault_tombstones")),
    )
    op.create_index(
        op.f("ix_vault_tombstones_domain_id_fingerprint"), TOMBSTONES, ["domain_id", "fingerprint"]
    )
    _isolate(TOMBSTONES)

    op.create_table(
        LINEAGE,
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("previous_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_vault_lineage_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "kind_id", "record_key", "previous_key", name=op.f("pk_vault_lineage")
        ),
    )
    op.create_index(
        op.f("ix_vault_lineage_domain_id_kind_id_previous_key"),
        LINEAGE,
        ["domain_id", "kind_id", "previous_key"],
    )
    _isolate(LINEAGE)

    op.create_table(
        SECRETS,
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_installation_secrets")),
    )
    # 244 random bits from the server's cryptographic generator (gen_random_uuid, Postgres 13+).
    op.execute(
        f"INSERT INTO {SECRETS} (name, secret) "
        "VALUES ('digest', replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''))"
    )


def downgrade() -> None:
    op.drop_table(SECRETS)
    op.drop_index(op.f("ix_vault_lineage_domain_id_kind_id_previous_key"), table_name=LINEAGE)
    op.drop_table(LINEAGE)
    op.drop_index(op.f("ix_vault_tombstones_domain_id_fingerprint"), table_name=TOMBSTONES)
    op.drop_table(TOMBSTONES)
    op.drop_column("vault", "fingerprint_key")
