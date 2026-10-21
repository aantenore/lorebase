"""Grants on the security definer read functions of revision 0004 (store schema notes).

The migration revokes EXECUTE from PUBLIC and grants it to `database.application_role`.
`grant_read_functions` grants it to another role later: one created after the migration, or a
new application role.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

READ_FUNCTIONS = ("lb_search", "lb_links_in", "lb_neighborhood", "lb_query_by_attribute")
"""Functions only the store read primitives call, with a Domain already authorized."""

_SIGNATURES = text(
    "SELECT CAST(CAST(p.oid AS regprocedure) AS text) FROM pg_proc p "
    "WHERE p.proname = ANY(CAST(:names AS text[])) "
    "AND p.pronamespace = CAST(current_schema() AS regnamespace) ORDER BY 1"
)
_QUOTED = text("SELECT quote_ident(:role)")


async def grant_read_functions(connection: AsyncConnection, role: str) -> list[str]:
    """Grant EXECUTE on the read functions to `role`; returns the signatures granted."""
    quoted = (await connection.execute(_QUOTED, {"role": role})).scalar_one()
    found = await connection.execute(_SIGNATURES, {"names": list(READ_FUNCTIONS)})
    signatures = list(found.scalars())
    for signature in signatures:
        await connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted}")
    return signatures
