"""Row-level security scope of the current transaction (store schema notes, RLS)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    if "," in domain_id:
        raise ValueError("Domain ids cannot contain commas")
    await session.execute(_ADD_DOMAIN, {"name": DOMAIN_IDS_SETTING, "domain": domain_id})
