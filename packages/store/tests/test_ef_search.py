"""`hnsw.ef_search` for over-fetching statements (`lorebase_store.embeddings.use_ef_search`)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from lorebase_store.commit import create_domain
from lorebase_store.embeddings import HNSW_MAX_EF_SEARCH, use_ef_search
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings

from .support import AUTHOR, DOMAIN, Clock, domain

pytestmark = pytest.mark.integration

DIMS = StoreSettings().embeddings.dims
ROWS = 120
NEAREST = f"""
SELECT count(*) FROM (
    SELECT object_hash FROM embeddings WHERE domain_id = :d AND dims = {DIMS}
    ORDER BY embedding::vector({DIMS}) <=> CAST(:q AS vector) LIMIT :n) nearest
"""


async def test_an_hnsw_scan_returns_the_rows_asked_for_only_with_a_raised_ef_search(
    engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await session.execute(
            text(
                "INSERT INTO embeddings "
                "(domain_id, object_hash, model, fingerprint, dims, embedding) "
                "SELECT :d, sha256(convert_to(i::text, 'UTF8')), 'm', 'f', :dims, "
                "       CAST(array_fill(0.01::real, ARRAY[:dims - 1]) || (i / 1000.0)::real "
                "            AS real[])::vector "
                "FROM generate_series(1, :n) i"
            ),
            {"d": DOMAIN, "dims": DIMS, "n": ROWS},
        )
    query = str([0.01] * (DIMS - 1) + [0.05])
    params = {"d": DOMAIN, "q": query, "n": ROWS}
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        await session.execute(text("SET LOCAL enable_sort = off"))
        # pgvector's default: the scan stops after 40 rows however many the LIMIT asks for.
        assert (await session.execute(text(NEAREST), params)).scalar_one() == 40
        assert await use_ef_search(session, ROWS) == ROWS
        assert (await session.execute(text(NEAREST), params)).scalar_one() == ROWS
        assert await use_ef_search(session, 10 * HNSW_MAX_EF_SEARCH) == HNSW_MAX_EF_SEARCH
        assert await use_ef_search(session, 0) == 1
    async with sessions() as session, session.begin():
        # SET LOCAL: the next transaction is back to the default.
        await scope_to_domain(session, DOMAIN)
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        await session.execute(text("SET LOCAL enable_sort = off"))
        assert (await session.execute(text(NEAREST), params)).scalar_one() == 40
