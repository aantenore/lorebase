"""Browser sessions of the web app, Keeper acting as backend-for-frontend (design 13.2).

One row per signed-in session. The cookie carries an opaque id; the row is found by the
SHA-256 of that id, which is never stored itself. `tokens` holds the provider's refresh and
ID tokens, encrypted by Keeper with a key from its configuration. The table is
installation-wide like `api_keys` (no `domain_id`, no policy); rows go with their principal.
Created by the owner role like every table: the application role receives row rights through
the owner's default privileges (deployment docs).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-22 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        sa.Column("id_hash", sa.LargeBinary(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column(
            "external_groups",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("tokens", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("octet_length(id_hash) = 32", name=op.f("ck_web_sessions_id_hash_h32")),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_web_sessions_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_web_sessions")),
    )
    op.create_index(
        op.f("ix_web_sessions_principal_id"), "web_sessions", ["principal_id"], unique=False
    )
    op.create_index(
        op.f("ix_web_sessions_last_seen_at"), "web_sessions", ["last_seen_at"], unique=False
    )
    op.create_index(
        op.f("ix_web_sessions_expires_at"), "web_sessions", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("web_sessions")
