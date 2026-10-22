"""The outbox against Postgres (design 10.5): every consumer owns its delivery state, an event
committed late is not skipped, pruning waits for every consumer, webhook deliveries are retried
with a stable idempotency key, and sensitive values reach only webhooks cleared for them."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Record, Sensitivity, vault_token
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordUpsert
from lorebase_store.auth.scope import rls_scope
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.outbox_events import operation_event_type, sensitive_value
from lorebase_store.repositories import scope_to_domain
from lorebase_store.repositories.events import (
    OutboxRepository,
    drop_consumer,
    register_consumer,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import OutboxSettings, StoreSettings
from lorebase_store.telemetry import OUTBOX_PRUNE_BACKLOG
from lorebase_store.vault import VaultSettings, erase_record
from lorebase_store.webhooks import (
    SigningKeys,
    WebhookRepository,
    WebhookSettings,
    deliver_pending,
    signing_key,
)
from lorebase_store.webhooks.delivery import WEBHOOKS_CONSUMER

from .support import (
    AUTHOR,
    DOMAIN,
    LEGAL_NAME,
    ORIGIN,
    SUPPLIER,
    SUPPLIER_CODE,
    Clock,
    attribute,
    domain,
    ident,
    supplier_kind,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
WRITES = StoreSettings().writes
SETTINGS = WebhookSettings(max_attempts=3, backoff_base_s=1, backoff_max_s=1)
EVENT = operation_event_type("risk_flag_raised")
EMAIL = ident("att", 190)
VAULT = VaultSettings.model_validate(
    {"token_key": "t" * 32, "encryption_key": "e" * 32, "key_id": "k1"}
)
SINK = "audit-sink"


class Sender:
    """Answers the statuses of `answers` in turn (then 204) and records every request."""

    def __init__(self, *answers: int) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        self.sent.append((url, json.loads(body), dict(headers)))
        return self.answers.pop(0) if self.answers else 204


async def public(host: str) -> Sequence[str]:
    return ["93.184.215.14"]


@pytest.fixture
def runtime(store: StoreSettings) -> StoreRuntime:
    return StoreRuntime.from_settings(store, vault=VAULT)


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    kind = supplier_kind()
    kind = kind.model_copy(
        update={
            "attributes": {
                **kind.attributes,
                EMAIL: attribute(
                    EMAIL,
                    "email",
                    TypeSpec.of("string"),
                    sensitivity=(Sensitivity.PII,),
                    vault=True,
                ),
            }
        }
    )
    clock = Clock()
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=clock)
    async with factory() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            author=AUTHOR,
            message="Schema",
            origin=ORIGIN,
            schema_changes=[SchemaUpsert(kind)],
            runtime=runtime,
            clock=clock,
        )
    await claim(factory, WEBHOOKS_CONSUMER)  # the Revision events of the setup commits
    return factory


async def emit(sessions: Sessions, payload: dict[str, Any] | None = None) -> int:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        return await OutboxRepository(session).enqueue(
            DOMAIN, EVENT, {"branch": "main", **(payload or {})}
        )


async def add_webhook(sessions: Sessions, host: str, clearances: Sequence[str] = ()) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await WebhookRepository(session, DOMAIN).add(
            url=f"https://{host}/hook",
            description=None,
            event_types=["operation.*"],
            branches=None,
            signing_key=SigningKeys(None).store(signing_key("secret")),
            created_by="usr_admin",
            clearances=clearances,
        )


async def claim(sessions: Sessions, consumer: str, *, ack: bool = True) -> list[int]:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        outbox = OutboxRepository(session)
        ids = [event.id for event in await outbox.claim(consumer, 100)]
        if ack:
            await outbox.acknowledge(consumer, ids)
        return ids


async def outbox_ids(sessions: Sessions) -> list[int]:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        return list((await session.execute(text("SELECT id FROM outbox ORDER BY id"))).scalars())


async def deliver(sessions: Sessions, send: Sender, runtime: StoreRuntime, **options: Any) -> Any:
    return await deliver_pending(
        sessions,
        SETTINGS,
        send,
        writes=WRITES,
        vault=runtime.vault,
        resolve=public,
        **options,
    )


async def test_each_consumer_keeps_its_own_state(sessions: Sessions) -> None:
    async with sessions() as session, session.begin():
        await register_consumer(session, SINK)
    first, second = await emit(sessions), await emit(sessions)
    assert await claim(sessions, WEBHOOKS_CONSUMER) == [first, second]
    assert await claim(sessions, WEBHOOKS_CONSUMER) == []
    # The webhook worker handing events off takes nothing from the other consumer.
    assert await claim(sessions, SINK, ack=False) == [first, second]
    assert await claim(sessions, SINK) == [first, second]
    assert await claim(sessions, SINK) == []
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        assert await OutboxRepository(session).pending() == {SINK: 0, WEBHOOKS_CONSUMER: 0}
        assert await drop_consumer(session, SINK)
        assert not await drop_consumer(session, SINK)


async def test_a_consumer_registered_later_starts_with_later_events(sessions: Sessions) -> None:
    before = await emit(sessions)
    async with sessions() as session, session.begin():
        await register_consumer(session, SINK)
    after = await emit(sessions)
    assert await claim(sessions, SINK) == [after]
    assert await claim(sessions, WEBHOOKS_CONSUMER) == [before, after]


async def test_an_event_committed_late_is_not_skipped(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    # The first event takes the lower id but commits after a later one was handed off.
    async with sessions() as slow:
        await slow.begin()
        await scope_to_domain(slow, DOMAIN)
        early = await OutboxRepository(slow).enqueue(DOMAIN, EVENT, {"branch": "main"})
        late = await emit(sessions)
        assert early < late
        assert await claim(sessions, WEBHOOKS_CONSUMER) == [late]
        await slow.commit()
    assert await claim(sessions, WEBHOOKS_CONSUMER) == [early]


async def test_a_failed_hand_off_leaves_the_events_pending(sessions: Sessions) -> None:
    event = await emit(sessions)

    async def hand_off_and_die() -> None:
        async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
            outbox = OutboxRepository(session)
            claimed = await outbox.claim(WEBHOOKS_CONSUMER, 10)
            await outbox.acknowledge(WEBHOOKS_CONSUMER, [e.id for e in claimed])
            raise RuntimeError("the worker died before committing")

    with pytest.raises(RuntimeError):
        await hand_off_and_die()
    assert await claim(sessions, WEBHOOKS_CONSUMER) == [event]


async def test_pruning_waits_for_every_consumer_until_events_expire(sessions: Sessions) -> None:
    async with sessions() as session, session.begin():
        await register_consumer(session, SINK)
    event = await emit(sessions)
    await claim(sessions, WEBHOOKS_CONSUMER)
    later = datetime.now(UTC) + timedelta(days=1)
    settings = OutboxSettings(retention_s=60, max_age_s=7 * 86400)

    async def prune(now: datetime) -> tuple[int, int]:
        async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
            return await OutboxRepository(session).prune(
                before=now - timedelta(seconds=settings.retention_s),
                expire_before=now - timedelta(seconds=settings.max_age_s),
                limit=settings.prune_batch,
            )

    # The setup's Revision events go; the event is still pending for the sink.
    assert (await prune(later))[1] == 0
    assert await outbox_ids(sessions) == [event]
    assert await prune(later + timedelta(days=7)) == (0, 1)  # expired all the same
    assert await outbox_ids(sessions) == []
    second = await emit(sessions)
    await claim(sessions, WEBHOOKS_CONSUMER)
    await claim(sessions, SINK)
    assert await prune(datetime.now(UTC)) == (0, 0)  # within retention
    assert await prune(later) == (1, 0)
    assert second not in await outbox_ids(sessions)


def backlog() -> int:
    (observation,) = OUTBOX_PRUNE_BACKLOG.observe()
    return int(observation.value)


async def test_a_pruning_pass_repeats_batches_until_nothing_is_left(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    for _ in range(7):
        await emit(sessions)
    await claim(sessions, WEBHOOKS_CONSUMER)
    total = len(await outbox_ids(sessions))
    assert total >= 7
    later = datetime.now(UTC) + timedelta(days=1)
    outbox = OutboxSettings(retention_s=60, max_age_s=7 * 86400, prune_batch=3)
    report = await deliver(sessions, Sender(), runtime, outbox=outbox, clock=lambda: later)
    assert report.pruned == total
    assert await outbox_ids(sessions) == []
    assert backlog() == 0


async def test_the_pruning_budget_stops_the_batches_and_the_backlog_is_reported(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    for _ in range(7):
        await emit(sessions)
    await claim(sessions, WEBHOOKS_CONSUMER)
    total = len(await outbox_ids(sessions))
    later = datetime.now(UTC) + timedelta(days=1)
    outbox = OutboxSettings(retention_s=60, max_age_s=7 * 86400, prune_batch=3, prune_budget_s=5)
    ticks = iter(range(0, 1000, 10))  # every reading is 10 s after the previous one
    report = await deliver(
        sessions,
        Sender(),
        runtime,
        outbox=outbox,
        clock=lambda: later,
        monotonic=lambda: float(next(ticks)),
    )
    assert report.pruned == 3  # one batch: the budget was spent by then
    assert len(await outbox_ids(sessions)) == total - 3
    assert backlog() == total - 3
    # The next pass, with time to spare, finishes the job.
    await deliver(sessions, Sender(), runtime, outbox=outbox, clock=lambda: later)
    assert await outbox_ids(sessions) == []
    assert backlog() == 0


async def test_deliveries_are_retried_with_one_idempotency_key_then_dead(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await add_webhook(sessions, "flaky.example.com")
    event = await emit(sessions, {"invocation_id": "inv_1"})
    send = Sender(500, 503, 500)
    now = datetime.now(UTC)
    first = await deliver(sessions, send, runtime, clock=lambda: now)
    assert (first.events, first.queued, first.retrying) == (1, 1, 1)
    second = await deliver(sessions, send, runtime, clock=lambda: now + timedelta(seconds=5))
    assert (second.events, second.retrying) == (0, 1)  # the event is not fanned out again
    third = await deliver(sessions, send, runtime, clock=lambda: now + timedelta(seconds=10))
    assert third.dead == 1
    keys = {headers["Idempotency-Key"] for _, _, headers in send.sent}
    assert keys == {f"evt_{event}"}
    assert {headers["Lorebase-Event"] for _, _, headers in send.sent} == {EVENT}
    assert [body["sequence"] for _, body, _ in send.sent] == [event] * 3
    after = await deliver(sessions, send, runtime, clock=lambda: now + timedelta(seconds=60))
    assert after.attempted == 0


async def test_sensitive_values_reach_only_cleared_webhooks(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [
                RecordUpsert(
                    record=Record(
                        kind=SUPPLIER,
                        key="S-1",
                        values={
                            SUPPLIER_CODE: "S-1",
                            LEGAL_NAME: "Acme",
                            EMAIL: "ada@example.com",
                        },
                    )
                )
            ],
            author=AUTHOR,
            message="Write",
            origin=ORIGIN,
            runtime=runtime,
        )
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        stored = await session.scalar(
            text("SELECT values FROM mat_records WHERE key = 'S-1' AND branch = 'main'")
        )
    reference = stored[EMAIL]
    assert vault_token(reference)
    payload = {
        "edits": [
            {
                "kind": "Supplier",
                "key": "S-1",
                "values": {
                    "legal_name": "Acme",
                    "email": sensitive_value([Sensitivity.PII], reference),
                    "vat": sensitive_value([Sensitivity.CONFIDENTIAL], "IT123"),
                    "both": sensitive_value([Sensitivity.PII, Sensitivity.RESTRICTED], "x"),
                },
            }
        ],
        "stray": reference,
    }
    await add_webhook(sessions, "plain.example.com")
    await add_webhook(sessions, "cleared.example.com", ["pii", "confidential"])
    await emit(sessions, payload)
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        stored_payloads = list(
            (await session.execute(text("SELECT payload::text FROM outbox"))).scalars()
        )
    assert not any("ada@example.com" in body for body in stored_payloads)

    send = Sender()
    await deliver(sessions, send, runtime)
    bodies = {urlsplit(url).hostname: body for url, body, _ in send.sent}
    plain = bodies["plain.example.com"]
    assert plain["edits"][0]["values"] == {
        "legal_name": "Acme",
        "email": "***",
        "vat": "***",
        "both": "***",
    }
    assert plain["stray"] == "***"
    cleared = bodies["cleared.example.com"]
    assert cleared["edits"][0]["values"] == {
        "legal_name": "Acme",
        "email": "ada@example.com",
        "vat": "IT123",
        "both": "***",
    }
    assert cleared["stray"] == "***"  # a reference outside a wrapper is never opened
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        stored_deliveries = list(
            (await session.execute(text("SELECT payload::text FROM webhook_deliveries"))).scalars()
        )
    assert not any("ada@example.com" in body for body in stored_deliveries)

    # An erasure before an attempt reaches the delivery.
    await emit(sessions, payload)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await erase_record(session, DOMAIN, SUPPLIER, "S-1")
    send = Sender()
    await deliver(sessions, send, runtime)
    erased = {urlsplit(url).hostname: body for url, body, _ in send.sent}["cleared.example.com"]
    assert erased["edits"][0]["values"]["email"] == "[erased]"
