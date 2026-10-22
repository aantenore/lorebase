"""A receiver that is gone or silent never holds a delivery pass: attempts time out and are
retried, deliveries a pass did not reach are released, and the lease of a stalled pass expires so
another pass takes its deliveries over."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.auth.scope import rls_scope
from lorebase_store.db import create_engine
from lorebase_store.repositories import scope_to_domain
from lorebase_store.settings import DatabaseSettings, StoreSettings
from lorebase_store.webhooks import (
    SigningKeys,
    WebhookRepository,
    WebhookSettings,
    deliver_pending,
    signing_key,
)
from lorebase_store.webhooks.delivery import DeliveryReport, _claim, _fan_out

pytestmark = pytest.mark.integration

DOMAIN = "acme"
EVENT = "test.event"
WRITES = StoreSettings().writes
FAST = WebhookSettings(timeout_s=0.3, connect_timeout_s=0.1, pass_timeout_s=1.0, lease_s=30)


class Sender:
    """Never answers for `gone.example.com`; answers 204 to the other hosts."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        if urlsplit(url).hostname == "gone.example.com":
            await asyncio.Event().wait()
        self.sent.append(url)
        return 204


async def public(host: str) -> Sequence[str]:
    return ["93.184.215.14"]


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO domains (id, display_name, leaf_size, created_at, created_by) "
                    "VALUES (:id, :id, 512, :at, 'usr_test')"
                ),
                {"id": DOMAIN, "at": datetime(2026, 10, 20, tzinfo=UTC)},
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def add_webhook(sessions: async_sessionmaker[AsyncSession], host: str) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await WebhookRepository(session, DOMAIN).add(
            url=f"https://{host}/hook",
            description=None,
            event_types=[EVENT],
            branches=None,
            signing_key=SigningKeys(None).store(signing_key("secret")),
            created_by="usr_admin",
        )


async def emit(sessions: async_sessionmaker[AsyncSession], count: int = 1) -> None:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        for _ in range(count):
            await session.execute(
                text(
                    "INSERT INTO outbox (domain_id, event_type, payload) "
                    "VALUES (:d, :e, CAST('{}' AS jsonb))"
                ),
                {"d": DOMAIN, "e": EVENT},
            )


async def deliveries(sessions: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        result = await session.execute(
            text(
                "SELECT d.id, w.url, d.status, d.attempts, d.last_error, d.next_attempt_at "
                "FROM webhook_deliveries d JOIN webhooks w "
                "ON w.domain_id = d.domain_id AND w.id = d.webhook_id ORDER BY d.id"
            )
        )
        return list(result)


async def test_a_silent_receiver_times_out_while_the_others_are_delivered(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await add_webhook(sessions, "gone.example.com")
    await add_webhook(sessions, "ok.example.com")
    await emit(sessions, count=2)
    send = Sender()
    loop = asyncio.get_running_loop()
    started = loop.time()
    report = await deliver_pending(sessions, FAST, send, writes=WRITES, resolve=public)
    assert loop.time() - started < FAST.pass_timeout_s + FAST.timeout_s + 1
    assert (report.delivered, report.retrying, report.timed_out, report.released) == (2, 2, 2, 0)
    rows = await deliveries(sessions)
    gone = [row for row in rows if "//gone." in row.url]
    assert {(row.status, row.attempts, row.last_error) for row in gone} == {
        ("pending", 1, "timed out after 0.3s")
    }
    assert {row.status for row in rows if "//ok." in row.url} == {"delivered"}


async def test_deliveries_a_pass_did_not_reach_are_due_again_untried(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await add_webhook(sessions, "gone.example.com")
    await emit(sessions, count=6)
    settings = FAST.model_copy(update={"concurrency": 1, "pass_timeout_s": 0.5})
    report = await deliver_pending(sessions, settings, Sender(), writes=WRITES, resolve=public)
    assert report.attempted + report.released == 6
    assert report.released >= 3
    now = datetime.now(UTC)
    untried = [row for row in await deliveries(sessions) if row.attempts == 0]
    assert len(untried) == report.released
    assert all(row.status == "pending" and row.next_attempt_at <= now for row in untried)


async def test_the_lease_of_a_stalled_pass_expires_and_another_pass_delivers(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await add_webhook(sessions, "ok.example.com")
    await emit(sessions)
    keys = SigningKeys.from_settings(FAST)
    now = datetime.now(UTC)
    # A pass hands the event off and claims the delivery, then its worker stalls for good.
    report = DeliveryReport()
    await _fan_out(sessions, FAST, WRITES, now, report)
    assert report.queued == 1
    assert len(await _claim(sessions, FAST, keys, now)) == 1
    assert await _claim(sessions, FAST, keys, now) == []  # leased: no one else takes it
    assert (await deliver_pending(sessions, FAST, Sender(), writes=WRITES)).attempted == 0
    later = now + timedelta(seconds=FAST.lease_s + 1)
    send = Sender()
    report = await deliver_pending(
        sessions, FAST, send, writes=WRITES, resolve=public, clock=lambda: later
    )
    assert report.delivered == 1
    assert send.sent == ["https://ok.example.com/hook"]
