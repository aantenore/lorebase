"""The audit log is append-only for every role but its owner.

The application role received `SELECT, INSERT, UPDATE, DELETE` on `audit_log` like on every
table (default privileges of the owner, deployment docs), though no code updates or deletes
audit entries: the hash chains are only extended. With those rights a compromised service
could rewrite or drop the newest entries of a chain without the verification noticing.

`UPDATE`, `DELETE` and `TRUNCATE` (and `REFERENCES`, `TRIGGER`, which nothing needs) are
revoked on `audit_log` from `PUBLIC`, from `database.application_role` when set and from
every other role holding them except the table owner, which runs the migrations and the
maintenance jobs. `SELECT` and `INSERT` stay. Nothing in Lorebase prunes the audit log; an
operator who needs to (retention, erasure) does it as the owner.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-23 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "audit_log"
REVOKED = "UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER"
RESTORED = "UPDATE, DELETE"
"""What the downgrade gives back to the application role: its rights before this revision."""


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
    """Roles other than the owner holding a revoked privilege on the table."""
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
            {"t": TABLE},
        )
        .scalars()
        .all()
    )


def upgrade() -> None:
    op.execute(f"REVOKE {REVOKED} ON TABLE {TABLE} FROM PUBLIC")
    roles = set(_holders())
    role = _application_role()
    if role is not None:
        roles.add(_quoted(role))
    for quoted in sorted(roles):
        op.execute(f"REVOKE {REVOKED} ON TABLE {TABLE} FROM {quoted}")


def downgrade() -> None:
    role = _application_role()
    if role is not None:
        op.execute(f"GRANT {RESTORED} ON TABLE {TABLE} TO {_quoted(role)}")
