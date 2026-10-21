"""Argument, scope and search_path checks in the security definer read functions.

Revision 0005 trusted its callers: a NULL count made `LIMIT NULL` (no limit), and the Domain
argument was not compared with the transaction scope. The four functions are replaced, with
the same signatures, bodies, owner and grants, by versions that first check:

- every count (`p_limit`, `p_candidates`, `p_depth`, `p_fanout`, `p_max_steps`, `p_max_nodes`)
  is between 1 and its ceiling in `store.reads.definer_ceilings`, read when this revision runs
  (SQLSTATE 22023 otherwise);
- `p_domain` is given (22023) and visible under the condition of the `domain_isolation`
  policies of revision 0001: listed in `lorebase.domain_ids` or `lorebase.bypass_rls` on
  (42501 otherwise);
- `search_path` is still the one fixed here, so `ALTER FUNCTION ... SET/RESET search_path`
  cannot change how the body resolves names (55000 otherwise).

The upgrade refuses to run while `database.application_role` may create objects in a schema
of that path (the schema of the tables, or of pg_trgm): such a role could shadow an operator
and run code as the function owner.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21 18:00:00.000000
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _revision_module(filename: str) -> ModuleType:
    """An earlier revision file of this directory, loaded for its SQL templates."""
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(f"_lorebase_revision_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_initial = _revision_module("0001_initial_schema.py")
_reads = _revision_module("0005_security_definer_reads.py")

COUNTS = {
    _reads.LB_SEARCH: ("p_candidates", "p_limit"),
    _reads.LB_LINKS_IN: ("p_limit",),
    _reads.LB_QUERY_BY_ATTRIBUTE: ("p_limit",),
    _reads.LB_NEIGHBORHOOD: ("p_depth", "p_fanout", "p_max_steps", "p_max_nodes"),
}
"""The 0005 template of each function and its count arguments, named as the fields of
`store.reads.definer_ceilings` with a `p_` prefix."""

_DOMAIN_VISIBLE, _replaced = re.subn(r"\bdomain_id\b", "p_domain", _initial.DOMAIN_VISIBLE)
if _replaced != 1:
    raise RuntimeError("revision 0001 changed its domain_isolation condition")

_SEARCH_PATH_CHECK = """
    IF pg_catalog.current_setting('search_path') OPERATOR(pg_catalog.<>) {path} THEN
        RAISE EXCEPTION 'the search_path of this function was changed from %', {path}
            USING ERRCODE = '55000';
    END IF;
"""
"""First, fully qualified: the rest of the body resolves names through the checked path."""

_DOMAIN_CHECK = f"""
    IF p_domain IS NULL OR p_domain = '' THEN
        RAISE EXCEPTION 'a Domain is required' USING ERRCODE = '22023';
    END IF;
    IF ({_DOMAIN_VISIBLE}) IS NOT TRUE THEN
        RAISE EXCEPTION 'Domain % is not in the transaction scope', p_domain
            USING ERRCODE = '42501';
    END IF;
"""

_COUNT_CHECK = """
    IF {name} IS NULL OR {name} NOT BETWEEN 1 AND {ceiling} THEN
        RAISE EXCEPTION '{name} must be between 1 and {ceiling}' USING ERRCODE = '22023';
    END IF;
"""


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_or_replace(template: str, guards: str = "") -> str:
    """A 0005 template as `CREATE OR REPLACE`, `guards` first in its body; the result still
    takes 0005's `options` and `record_columns`."""
    statement = template.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    if guards:
        escaped = guards.replace("{", "{{").replace("}", "}}")
        statement = statement.replace("\nBEGIN\n", "\nBEGIN" + escaped, 1)
    return statement


def _guards(search_path: str, counts: Sequence[str]) -> str:
    ceilings = op.get_context().config.attributes["store_settings"].reads.definer_ceilings
    checks = [_SEARCH_PATH_CHECK.format(path=_literal(search_path)), _DOMAIN_CHECK]
    for name in counts:
        ceiling = getattr(ceilings, name.removeprefix("p_"))
        checks.append(_COUNT_CHECK.format(name=name, ceiling=int(ceiling)))
    return "".join(checks)


def _refuse_writable_schemas(role: str) -> None:
    """Refuse when `role` may CREATE in the schema of the tables or of pg_trgm."""
    writable = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT n.nspname FROM pg_namespace n "
                "WHERE (n.nspname = current_schema() OR n.oid = (SELECT extnamespace "
                "FROM pg_extension WHERE extname = 'pg_trgm')) "
                "AND has_schema_privilege(:role, n.oid, 'CREATE') ORDER BY 1"
            ),
            {"role": role},
        )
        .scalars()
        .all()
    )
    if writable:
        raise RuntimeError(
            f"database.application_role {role!r} may create objects in schema(s) "
            f"{', '.join(writable)}, which the security definer read functions resolve names "
            "in; revoke CREATE on them from the role (and from PUBLIC) and migrate again"
        )


def _replace(*, checked: bool) -> None:
    search_path = _reads._search_path()
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    for template, counts in COUNTS.items():
        guards = _guards(search_path, counts) if checked else ""
        statement = _create_or_replace(template, guards)
        op.execute(statement.format(options=options, record_columns=_reads._RECORD_COLUMNS))
    # CREATE OR REPLACE keeps the owner and the grants; restate them all the same.
    for signature in _reads.SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    role = _reads._application_role()
    if role is not None:
        quoted = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar()
        for signature in _reads.SIGNATURES:
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted}")


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        exists = (
            op.get_bind()
            .execute(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": role}
            )
            .scalar()
        )
        if not exists:
            raise RuntimeError(
                f"database.application_role names {role!r}, which does not exist; create the "
                "role first or leave the setting empty"
            )
        _refuse_writable_schemas(role)
    _replace(checked=True)


def downgrade() -> None:
    _replace(checked=False)
