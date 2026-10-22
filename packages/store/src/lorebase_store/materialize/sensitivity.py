"""Sensitivity tags of the default branch, for what every branch materializes (design 10.2).

Titles and full-text vectors leave sensitive Attributes out. A branch forked before the
default branch tagged an Attribute still has it untagged in its own schema, so the
materializer, the rebuild and the verification add the default branch's tags
(`lorebase_core.expr.sensitivity.with_tags`) when they compute titles and search vectors.
The default branch's schema comes from the store's schema cache, keyed by its head Revision.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.expr.sensitivity import Tags, schema_tags
from lorebase_core.model import Schema
from lorebase_store.reads import head_schema
from lorebase_store.repositories.domains import DomainRepository
from lorebase_store.runtime import StoreRuntime


async def default_branch_schema(
    session: AsyncSession, domain_id: str, *, runtime: StoreRuntime
) -> Schema | None:
    """The schema at the head of the Domain's default branch (cached by head Revision); None
    when the Domain or its materialized head is missing."""
    domain = await DomainRepository(session).get(domain_id)
    if domain is None:
        return None
    found = await head_schema(session, domain_id, domain.default_branch, runtime=runtime)
    return None if found is None else found.schema


async def reference_tags(
    session: AsyncSession, domain_id: str, branch: str, *, runtime: StoreRuntime
) -> dict[str, Tags]:
    """Tags of the default branch's schema by Attribute id; empty on the default branch
    itself (its own schema decides) or when it is not materialized."""
    domain = await DomainRepository(session).get(domain_id)
    if domain is None or branch == domain.default_branch:
        return {}
    found = await head_schema(session, domain_id, domain.default_branch, runtime=runtime)
    return {} if found is None else schema_tags(found.schema)
