"""Structured output modes learned per chat endpoint.

`inference_endpoint_modes` remembers the mode (`json_schema`, `json_mode` or `prompt`) that a
chat endpoint was found to need after it rejected a stronger one, so a restarted process does
not retry the stronger ones; `updated_at` lets readers expire an entry. Keyed by a keyed digest
of the endpoint, model and output settings; no Domain data. The application role may read,
insert, update and delete rows, nothing else. The downgrade drops the table: the modes are
learned again from the next rejections.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-28 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODES = "inference_endpoint_modes"
REVOKED = "TRUNCATE, REFERENCES, TRIGGER"
GRANTED = "SELECT, INSERT, UPDATE, DELETE"


def _application_role() -> str | None:
    config = op.get_context().config
    if config is None or "database_settings" not in config.attributes:
        raise RuntimeError("run this migration through the Lorebase Alembic environment")
    role: str | None = config.attributes["database_settings"].application_role
    return role


def _quoted(role: str) -> str:
    quoted: str = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar_one()
    return quoted


def upgrade() -> None:
    op.create_table(
        MODES,
        sa.Column("key", sa.LargeBinary(), nullable=False),
        sa.Column("mode_index", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(key) = 32", name=op.f("ck_inference_endpoint_modes_key_h32")
        ),
        sa.CheckConstraint(
            "mode_index BETWEEN 0 AND 2", name=op.f("ck_inference_endpoint_modes_mode_index")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_inference_endpoint_modes")),
    )
    op.execute(f"REVOKE ALL ON TABLE {MODES} FROM PUBLIC")
    role = _application_role()
    if role is not None:
        quoted = _quoted(role)
        op.execute(f"REVOKE {REVOKED} ON TABLE {MODES} FROM {quoted}")
        op.execute(f"GRANT {GRANTED} ON TABLE {MODES} TO {quoted}")


def downgrade() -> None:
    op.drop_table(MODES)
