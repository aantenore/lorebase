"""Row-level security scope of the current transaction (store schema notes, RLS)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import checked_domain_id

DOMAIN_IDS_SETTING = "lorebase.domain_ids"
"""Transaction setting listing, comma-separated, the Domains the transaction may touch."""

_ADD_DOMAIN = text(
    """
    SELECT set_config(
        :name,
        CASE
            WHEN coalesce(current_setting(:name, true), '') = '' THEN :domain
            WHEN :domain = ANY(string_to_array(current_setting(:name, true), ','))
                THEN current_setting(:name, true)
            ELSE current_setting(:name, true) || ',' || :domain
        END,
        true
    )
    """
)


async def scope_to_domain(session: AsyncSession, domain_id: str) -> None:
    """Add `domain_id` to the Domains visible in the current transaction."""
    await session.execute(
        _ADD_DOMAIN, {"name": DOMAIN_IDS_SETTING, "domain": checked_domain_id(domain_id)}
    )
