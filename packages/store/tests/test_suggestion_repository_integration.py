"""Concurrency of `SchemaSuggestionRepository.record` and `merge_variants` against Postgres."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.infer.suggestions import Observation, SuggestionOptions, SuggestionType
from lorebase_store.db import create_engine
from lorebase_store.repositories import SchemaSuggestionRepository, scope_to_domain
from lorebase_store.repositories import suggestions as module
from lorebase_store.repositories.suggestions import SUGGESTIONS
from lorebase_store.settings import DatabaseSettings

pytestmark = pytest.mark.integration

DOMAIN = "supply"
NOW = datetime(2026, 10, 20, tzinfo=UTC)
OPTIONS = SuggestionOptions()
KIND = "knd_contract"


@pytest.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO domains (id, display_name, leaf_size, created_at, created_by) "
                "VALUES (:id, :id, 512, :at, 'usr_test')"
            ),
            {"id": DOMAIN, "at": NOW},
        )
    return async_sessionmaker(engine, expire_on_commit=False)


async def seed(
    sessions: async_sessionmaker[AsyncSession], name: str, support: int, *, document: str = "doc_1"
) -> str:
    """A stored pending Attribute suggestion of `name`, written as an older release would."""
    fingerprint = f"attribute:{KIND}:{name}"
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await session.execute(
            SUGGESTIONS.insert().values(
                domain_id=DOMAIN,
                id=f"ssg_{name}",
                fingerprint=fingerprint,
                suggestion_type="attribute",
                name=name,
                kind_ref=KIND,
                labels=[name],
                support=support,
                confidence=0.6,
                observations={document: {"n": support, "c": 0.6 * support}},
                examples=[],
                samples=["90"],
                sources=["rules"],
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return fingerprint


async def supports(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        rows = await SchemaSuggestionRepository(session, DOMAIN).page(limit=100)
    return {r.name: r.support for r in rows}


def observation(name: str, document: str) -> Observation:
    return Observation(
        SuggestionType.ATTRIBUTE, name, document, 0.6, "rules", kind=KIND, value="90"
    )


async def merge(sessions: async_sessionmaker[AsyncSession], **extra: Any) -> module.MergeOutcome:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        return await SchemaSuggestionRepository(session, DOMAIN).merge_variants(
            options=OPTIONS, now=NOW, **extra
        )


async def test_record_and_merge_variants_run_together_without_deadlock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    for i in range(6):
        await seed(sessions, f"renewal_notice_days_v{i}", support=i + 1)
    ids = count()

    async def record(n: int) -> None:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await SchemaSuggestionRepository(session, DOMAIN).record(
                [observation(f"renewal_notice_days_v{n % 6}", f"doc_new_{n}")],
                job_id="exj_1",
                options=OPTIONS,
                now=NOW,
                new_id=lambda: f"ssg_new{next(ids)}",
            )

    work = [merge(sessions), *(record(n) for n in range(8)), merge(sessions)]
    await asyncio.wait_for(asyncio.gather(*work), timeout=30)
    assert sum((await supports(sessions)).values()) == 21 + 8  # nothing lost to either writer


async def test_record_locks_the_pending_set_before_consolidating(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    fingerprint = await seed(sessions, "renewal_notice_days", support=4)
    holder = sessions()
    await holder.begin()
    await scope_to_domain(holder, DOMAIN)
    await holder.execute(
        select(SUGGESTIONS.c.id).where(SUGGESTIONS.c.fingerprint == fingerprint).with_for_update()
    )

    async def record() -> None:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await SchemaSuggestionRepository(session, DOMAIN).record(
                [observation("renewal proposal notice days", "doc_2")],
                job_id="exj_2",
                options=OPTIONS,
                now=NOW,
                new_id=lambda: "ssg_x",
            )

    task = asyncio.create_task(record())
    try:
        await asyncio.sleep(0.5)
        assert not task.done()  # waits for the pending row instead of reading it stale
    finally:
        await holder.commit()
        await holder.close()
    await asyncio.wait_for(task, timeout=15)
    assert await supports(sessions) == {"renewal_notice_days": 5}


async def test_merge_variants_holds_no_row_lock_while_it_computes_and_skips_changed_sets(
    sessions: async_sessionmaker[AsyncSession],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(sessions, "renewal_notice_days", support=4)
    await seed(sessions, "renewal_proposal_notice_days", support=2, document="doc_2")
    real = module.merge_attributes
    written: list[bool] = []

    def writer_thread() -> None:
        async def write() -> None:
            engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
            try:
                async with async_sessionmaker(engine)() as session, session.begin():
                    await scope_to_domain(session, DOMAIN)
                    await session.execute(
                        SUGGESTIONS.update()
                        .where(SUGGESTIONS.c.name == "renewal_proposal_notice_days")
                        .values(
                            support=3,
                            observations={"doc_2": {"n": 3, "c": 1.8}},
                            updated_at=datetime(2026, 10, 21, tzinfo=UTC),
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(write())
        written.append(True)

    def computing(*args: Any, **kwargs: Any) -> dict[str, str]:
        # A writer commits while the merge computes; it would block here if rows were locked.
        if not written:
            thread = threading.Thread(target=writer_thread)
            thread.start()
            thread.join(timeout=15)
            assert written, "the writer waited for a lock held by the merge"
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "merge_attributes", computing)
    first = await merge(sessions, max_passes=1)
    assert first.merged == 0  # the set changed after it was read: left for the next pass
    assert await supports(sessions) == {"renewal_notice_days": 4, "renewal_proposal_notice_days": 3}
    second = await merge(sessions, max_passes=1)
    assert (second.merged, second.kept) == (1, 1)
    assert await supports(sessions) == {"renewal_notice_days": 7}


async def test_merge_variants_folds_stored_variants(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    for i, name in enumerate(["renewal_notice_days", "renewal_proposal_notice_days"]):
        await seed(sessions, name, support=5 - i)
    outcome = await merge(sessions)
    assert outcome.merged == 1
    assert await supports(sessions) == {"renewal_notice_days": 9}


class _VictimError(Exception):
    sqlstate = "40P01"


async def test_a_deadlock_victim_is_retried_once(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(sessions, "renewal_notice_days", support=4)
    await seed(sessions, "renewal_proposal_notice_days", support=2, document="doc_2")
    real = SchemaSuggestionRepository._merge_pass
    calls: list[int] = []

    async def flaky(self: Any, options: Any, now: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise DBAPIError("SELECT", {}, _VictimError("deadlock detected"))
        return await real(self, options, now)

    monkeypatch.setattr(SchemaSuggestionRepository, "_merge_pass", flaky)
    outcome = await merge(sessions, max_passes=1)
    assert outcome.merged == 1
    assert len(calls) == 2

    calls.clear()

    async def always(self: Any, options: Any, now: Any) -> Any:
        calls.append(1)
        raise DBAPIError("SELECT", {}, _VictimError("deadlock detected"))

    monkeypatch.setattr(SchemaSuggestionRepository, "_merge_pass", always)
    with pytest.raises(DBAPIError):
        await merge(sessions, max_passes=1)
    assert len(calls) == 2  # once more, not forever
