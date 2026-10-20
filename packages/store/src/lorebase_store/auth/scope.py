"""Row-level security scope for auth lookups (see docs/store-schema-notes.md).

Postgres policies read two transaction-local settings: `lorebase.domain_ids` (comma-separated
Domain ids) and `lorebase.bypass_rls` (`on` for system paths). `rls_scope` sets them for the
body of the `with` block and puts the previous values back afterwards, so repositories can be
used inside a longer transaction without widening what later statements see.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DOMAIN_IDS_SETTING = "lorebase.domain_ids"
BYPASS_SETTING = "lorebase.bypass_rls"

_SWAP = text(
    "WITH previous AS MATERIALIZED ("
    "SELECT coalesce(current_setting(:domains_name, true), '') AS domains, "
    "coalesce(current_setting(:bypass_name, true), '') AS bypass) "
    "SELECT previous.domains, previous.bypass, "
    "set_config(:domains_name, :domains, true), set_config(:bypass_name, :bypass, true) "
    "FROM previous"
)
_RESTORE = text(
    "SELECT set_config(:domains_name, :domains, true), set_config(:bypass_name, :bypass, true)"
)


@asynccontextmanager
async def rls_scope(
    session: AsyncSession, *, domain_ids: Sequence[str] = (), bypass: bool = False
) -> AsyncIterator[None]:
    """Make rows of `domain_ids` (or every row with `bypass`) visible inside the block.

    The settings are transaction-local. When the block raises, they are left as they are:
    the transaction is expected to roll back.
    """
    names = {"domains_name": DOMAIN_IDS_SETTING, "bypass_name": BYPASS_SETTING}
    row = (
        await session.execute(
            _SWAP, {**names, "domains": ",".join(domain_ids), "bypass": "on" if bypass else ""}
        )
    ).one()
    yield
    await session.execute(_RESTORE, {**names, "domains": row.domains, "bypass": row.bypass})


def new_id(prefix: str) -> str:
    """Application-generated id with a type prefix, e.g. `usr_3f2a...`."""
    return f"{prefix}_{uuid.uuid4().hex}"
