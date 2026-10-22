"""Webhook signing keys at rest (revision 0016): sealed when a key is configured, sealed by the
worker when stored before, opened under previous keys, and refused under unknown ones."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime

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
    new_secret,
    signing_key,
    verify,
)

pytestmark = pytest.mark.integration

DOMAIN = "acme"
EVENT = "test.event"
KEY_A = "a" * 32
KEY_B = "b" * 32
KEY_C = "c" * 32
WRITES = StoreSettings().writes


class Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, dict[str, str]]] = []

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        self.sent.append((body, dict(headers)))
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


def settings(current: str | None = None, *previous: str) -> WebhookSettings:
    return WebhookSettings.model_validate(
        {"encryption_key": current, "previous_encryption_keys": list(previous)}
    )


async def add_webhook(
    sessions: async_sessionmaker[AsyncSession], secret: str, keys: SigningKeys
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await WebhookRepository(session, DOMAIN).add(
            url="https://hooks.example.com/x",
            description=None,
            event_types=[EVENT],
            branches=None,
            signing_key=keys.store(signing_key(secret)),
            created_by="usr_admin",
        )


async def emit(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        await session.execute(
            text(
                "INSERT INTO outbox (domain_id, event_type, payload) "
                "VALUES (:d, :e, CAST('{}' AS jsonb))"
            ),
            {"d": DOMAIN, "e": EVENT},
        )


async def stored(sessions: async_sessionmaker[AsyncSession]) -> tuple[bool, bool]:
    """Whether the webhook's key is stored usable, and whether sealed."""
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        row = (
            await session.execute(
                text("SELECT secret_hash IS NOT NULL, sealed_key IS NOT NULL FROM webhooks")
            )
        ).one()
    return bool(row[0]), bool(row[1])


async def deliver(
    sessions: async_sessionmaker[AsyncSession], configured: WebhookSettings
) -> tuple[Sender, int, int]:
    send = Sender()
    await emit(sessions)
    report = await deliver_pending(sessions, configured, send, writes=WRITES, resolve=public)
    return send, report.sealed, report.delivered


def signed_by(secret: str, send: Sender) -> bool:
    ((body, headers),) = send.sent
    return verify(
        secret, headers["Lorebase-Signature"], body, now=int(time.time()), tolerance_s=300
    )


async def test_a_key_stored_before_is_sealed_and_still_signs(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    secret = new_secret(32)
    await add_webhook(sessions, secret, SigningKeys(None))
    assert await stored(sessions) == (True, False)

    send, sealed, delivered = await deliver(sessions, settings(KEY_A))
    assert (sealed, delivered) == (1, 1)
    assert await stored(sessions) == (False, True)
    assert signed_by(secret, send)


async def test_new_keys_are_sealed_and_rotate_to_the_current_key(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    secret = new_secret(32)
    await add_webhook(sessions, secret, SigningKeys(KEY_A))
    assert await stored(sessions) == (False, True)

    send, sealed, delivered = await deliver(sessions, settings(KEY_B, KEY_A))
    assert (sealed, delivered) == (1, 1)
    assert signed_by(secret, send)

    send, sealed, delivered = await deliver(sessions, settings(KEY_B))
    assert (sealed, delivered) == (0, 1)
    assert signed_by(secret, send)


async def test_a_key_sealed_under_an_unknown_key_fails_the_attempt(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await add_webhook(sessions, new_secret(32), SigningKeys(KEY_A))
    send, _, delivered = await deliver(sessions, settings(KEY_C))
    assert (delivered, send.sent) == (0, [])
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        status, error = (
            await session.execute(text("SELECT status, last_error FROM webhook_deliveries"))
        ).one()
    assert status == "pending"
    assert "webhooks.encryption_key" in error
