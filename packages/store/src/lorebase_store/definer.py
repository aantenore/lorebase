"""Grants on the security definer read functions of revisions 0005, 0010, 0018, 0035 and 0037
(store schema notes).

The migration revokes EXECUTE from PUBLIC and grants it to `database.application_role`.
`grant_read_functions` grants it to another role later: one created after the migration, or a
new application role.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

READ_FUNCTIONS = (
    "lb_search",
    "lb_links_in",
    "lb_neighborhood",
    "lb_query_by_attribute",
    "lb_search_overlay",
    "lb_links_in_overlay",
    "lb_neighborhood_overlay",
    "lb_query_by_attribute_overlay",
    "lb_search_corrections",
    "lb_search_corrected",
    "lb_search_corrected_overlay",
    "lb_search_bounded",
    "lb_search_bounded_overlay",
)
"""Functions only the store read primitives call, with a Domain already authorized."""
BM25_FUNCTIONS = ("lb_search_bm25", "lb_search_bm25_overlay")
"""The BM25 search of revision 0037, called the same way; they reach the BM25 index only
where `pg_search` is installed (`lorebase_store.bm25`)."""

_SIGNATURES = text(
    "SELECT CAST(CAST(p.oid AS regprocedure) AS text) FROM pg_proc p "
    "WHERE p.proname = ANY(CAST(:names AS text[])) "
    "AND p.pronamespace = CAST(current_schema() AS regnamespace) ORDER BY 1"
)
_QUOTED = text("SELECT quote_ident(:role)")


async def grant_read_functions(connection: AsyncConnection, role: str) -> list[str]:
    """Grant EXECUTE on the read functions to `role`; returns the signatures granted."""
    quoted = (await connection.execute(_QUOTED, {"role": role})).scalar_one()
    names = [*READ_FUNCTIONS, *BM25_FUNCTIONS]
    found = await connection.execute(_SIGNATURES, {"names": names})
    signatures = list(found.scalars())
    for signature in signatures:
        await connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted}")
    return signatures
