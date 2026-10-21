"""Webhook delivery from the outbox (design 10.5): `deliver_pending`, one worker pass.

A pass, safe to run concurrently in several workers (rows are claimed with `SKIP LOCKED`):

1. fan out: the oldest undelivered outbox events become one delivery per matching webhook
   (event type, branch, created before the event); the event is marked handed off;
2. claim the due deliveries, leasing them for `lease_s`;
3. send each one as an HMAC-signed JSON POST (`webhooks.signing`) through the injected
   `Sender`; a 2xx answer delivers it, anything else is retried after
   `min(backoff_base_s * backoff_factor ** (attempts - 1), backoff_max_s)` until
   `max_attempts`, then marked dead. Targets outside `allowed_schemes`, or resolving to
   private addresses unless `allow_private_targets`, are marked dead at once. The host is
   resolved once per attempt and the `Sender` connects to those checked addresses only, so a
   second lookup (DNS rebinding) cannot redirect the request to an internal service;
4. prune finished deliveries and handed-off events older than `retention_s`.

The store makes no HTTP calls itself: the caller (Scribe, or a test) passes the `Sender`.
Reads span every Domain, so the pass runs with row-level security bypassed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_store.auth.scope import rls_scope
from lorebase_store.changefeed import EventRef, branch_schema, describe
from lorebase_store.commit.pipeline import COMMITTED_EVENT
from lorebase_store.repositories.events import OutboxRepository, OutboxRow
from lorebase_store.settings import WriteSettings
from lorebase_store.webhooks.repository import WEBHOOK_DELIVERIES, WEBHOOKS, webhook_row
from lorebase_store.webhooks.settings import WebhookSettings
from lorebase_store.webhooks.signing import signature

logger = logging.getLogger("lorebase.store.webhooks")

PENDING, DELIVERED, DEAD = "pending", "delivered", "dead"
ERROR_LENGTH = 500
"""Characters of an error message kept on a delivery."""


class Sender(Protocol):
    """Sends one POST and answers its HTTP status; transport failures raise.

    `addresses` are the checked addresses of the URL's host: the sender connects to one of
    them (keeping the host name for the `Host` header and TLS) and never resolves the host
    again, and it does not follow redirects. Empty when private targets are allowed: the
    sender then resolves the host itself.
    """

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int: ...


Resolver = Callable[[str], Awaitable[Sequence[str]]]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


async def resolve_host(host: str) -> Sequence[str]:
    """Addresses of a host name."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _private(address: str) -> bool:
    """Whether an address is not a plain public unicast one.

    Anything outside the globally routable ranges is refused, not only RFC 1918 and loopback:
    shared address space (100.64.0.0/10, where some clouds serve instance metadata), IPv6
    forms that wrap an IPv4 address (mapped, 6to4, Teredo, NAT64 under the reserved ::/8),
    site-local, multicast and reserved ranges.
    """
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    site_local = isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local
    return not ip.is_global or ip.is_multicast or ip.is_reserved or site_local


def scheme_problem(url: str, settings: WebhookSettings) -> str | None:
    """Why `url` cannot be a webhook target by its form alone; None when it can."""
    parts = urlsplit(url)
    if parts.scheme not in settings.allowed_schemes:
        return f"scheme must be one of {', '.join(settings.allowed_schemes)}"
    if not parts.hostname:
        return "has no host"
    if not settings.allow_private_targets:
        try:
            if _private(parts.hostname):
                return "targets a private address"
        except ValueError:
            if parts.hostname == "localhost" or parts.hostname.endswith(".localhost"):
                return "targets a private address"
    return None


@dataclass(frozen=True, slots=True)
class Target:
    """A checked target: why it is refused, or the addresses a request may connect to."""

    problem: str | None
    addresses: tuple[str, ...] = ()


async def check_target(url: str, settings: WebhookSettings, resolve: Resolver) -> Target:
    """Resolve the host of `url` once and check its form and every address it resolves to.

    The addresses returned are the only ones a request to `url` may use: resolving the host
    again could answer differently (DNS rebinding). None are returned when private targets
    are allowed.
    """
    problem = scheme_problem(url, settings)
    if problem is not None or settings.allow_private_targets:
        return Target(problem)
    host = urlsplit(url).hostname or ""
    try:
        addresses = tuple(await resolve(host))
    except OSError as error:
        return Target(f"cannot resolve host: {error}")
    if not addresses:
        return Target("cannot resolve host: no addresses")
    try:
        if any(_private(address) for address in addresses):
            return Target("resolves to a private address")
    except ValueError:
        return Target("resolves to an invalid address")
    return Target(None, addresses)


async def target_problem(url: str, settings: WebhookSettings, resolve: Resolver) -> str | None:
    """Why `url` may not be called now (form, or private addresses after resolution)."""
    return (await check_target(url, settings, resolve)).problem


def backoff(attempts: int, settings: WebhookSettings) -> timedelta:
    """Delay before the next attempt after `attempts` failed ones."""
    seconds = settings.backoff_base_s * settings.backoff_factor ** max(attempts - 1, 0)
    return timedelta(seconds=min(seconds, settings.backoff_max_s))


@dataclass
class DeliveryReport:
    events: int = 0
    """Outbox events handed off."""
    queued: int = 0
    """Deliveries created from them."""
    attempted: int = 0
    delivered: int = 0
    retrying: int = 0
    dead: int = 0
    pruned: int = 0


@dataclass(frozen=True, slots=True)
class _Claimed:
    domain_id: str
    id: int
    url: str
    secret_hash: bytes
    event_type: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class _Outcome:
    claimed: _Claimed
    status: str
    http_status: int | None
    error: str | None


async def _payload(
    session: AsyncSession,
    event: OutboxRow,
    schemas: dict[tuple[str, str], Schema | None],
    settings: WebhookSettings,
    writes: WriteSettings,
) -> dict[str, Any]:
    if event.event_type != COMMITTED_EVENT:
        return {"type": event.event_type, "sequence": event.id, **event.payload}
    branch = str(event.payload.get("branch", ""))
    if (event.domain_id, branch) not in schemas:
        schemas[(event.domain_id, branch)] = await branch_schema(
            session, event.domain_id, branch, writes=writes
        )
    documents = await describe(
        session,
        event.domain_id,
        [EventRef(bytes.fromhex(event.payload["revision"]), branch, event.id)],
        schemas={branch: schemas[(event.domain_id, branch)]},
        keys_max=settings.changed_keys_max,
    )
    if documents:
        return documents[0]
    return {"type": event.event_type, "sequence": event.id, **event.payload}


async def _fan_out(
    sessions: async_sessionmaker[AsyncSession],
    settings: WebhookSettings,
    writes: WriteSettings,
    now: datetime,
    report: DeliveryReport,
) -> None:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        outbox = OutboxRepository(session)
        events = await outbox.claim_undelivered(settings.batch)
        if not events:
            return
        result = await session.execute(
            select(*WEBHOOKS.c).where(WEBHOOKS.c.domain_id.in_({e.domain_id for e in events}))
        )
        hooks = defaultdict(list)
        for row in result:
            hooks[row.domain_id].append(webhook_row(row))
        schemas: dict[tuple[str, str], Schema | None] = {}
        rows: list[dict[str, Any]] = []
        for event in events:
            branch = event.payload.get("branch")
            targets = [
                hook
                for hook in hooks.get(event.domain_id, [])
                if event.event_type in hook.event_types
                and (hook.branches is None or branch in hook.branches)
                and hook.created_at <= event.created_at
            ]
            if not targets:
                continue
            payload = await _payload(session, event, schemas, settings, writes)
            rows.extend(
                {
                    "domain_id": event.domain_id,
                    "webhook_id": hook.id,
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "payload": payload,
                    "next_attempt_at": now,
                }
                for hook in targets
            )
        if rows:
            await session.execute(insert(WEBHOOK_DELIVERIES).values(rows).on_conflict_do_nothing())
        await outbox.mark_delivered([e.id for e in events], now)
        report.events += len(events)
        report.queued += len(rows)


async def _claim(
    sessions: async_sessionmaker[AsyncSession], settings: WebhookSettings, now: datetime
) -> list[_Claimed]:
    d, w = WEBHOOK_DELIVERIES, WEBHOOKS
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        due = list(
            await session.execute(
                select(d.c.domain_id, d.c.id)
                .where(d.c.status == PENDING, d.c.next_attempt_at <= now)
                .order_by(d.c.next_attempt_at, d.c.id)
                .limit(settings.batch)
                .with_for_update(skip_locked=True)
            )
        )
        if not due:
            return []
        keys = [(row.domain_id, row.id) for row in due]
        await session.execute(
            update(d)
            .where(tuple_(d.c.domain_id, d.c.id).in_(keys))
            .values(next_attempt_at=now + timedelta(seconds=settings.lease_s))
        )
        result = await session.execute(
            select(
                d.c.domain_id,
                d.c.id,
                w.c.url,
                w.c.secret_hash,
                d.c.event_type,
                d.c.payload,
                d.c.attempts,
            )
            .join(w, and_(w.c.domain_id == d.c.domain_id, w.c.id == d.c.webhook_id))
            .where(tuple_(d.c.domain_id, d.c.id).in_(keys))
            .order_by(d.c.id)
        )
        return [
            _Claimed(
                row.domain_id,
                row.id,
                row.url,
                bytes(row.secret_hash),
                row.event_type,
                row.payload,
                row.attempts,
            )
            for row in result
        ]


async def _attempt(
    claimed: _Claimed,
    settings: WebhookSettings,
    send: Sender,
    clock: Clock,
    resolve: Resolver,
) -> _Outcome:
    target = await check_target(claimed.url, settings, resolve)
    if target.problem is not None:
        return _Outcome(claimed, DEAD, None, f"target refused: {target.problem}")
    body = json.dumps(claimed.payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        settings.signature_header: signature(claimed.secret_hash, int(clock().timestamp()), body),
        settings.event_header: claimed.event_type,
        settings.delivery_header: str(claimed.id),
    }
    try:
        status = await send(claimed.url, body, headers, settings.timeout_s, target.addresses)
    except Exception as error:  # any transport failure is retried
        message = f"{type(error).__name__}: {error}"[:ERROR_LENGTH]
        return _Outcome(claimed, _after_failure(claimed, settings), None, message)
    if 200 <= status < 300:
        return _Outcome(claimed, DELIVERED, status, None)
    return _Outcome(claimed, _after_failure(claimed, settings), status, f"HTTP {status}")


def _after_failure(claimed: _Claimed, settings: WebhookSettings) -> str:
    return DEAD if claimed.attempts + 1 >= settings.max_attempts else PENDING


async def _record(
    sessions: async_sessionmaker[AsyncSession],
    outcomes: Sequence[_Outcome],
    settings: WebhookSettings,
    now: datetime,
) -> None:
    d = WEBHOOK_DELIVERIES
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        for outcome in outcomes:
            attempts = outcome.claimed.attempts + 1
            await session.execute(
                update(d)
                .where(d.c.domain_id == outcome.claimed.domain_id, d.c.id == outcome.claimed.id)
                .values(
                    status=outcome.status,
                    attempts=attempts,
                    last_status=outcome.http_status,
                    last_error=outcome.error,
                    next_attempt_at=now + backoff(attempts, settings),
                    delivered_at=now if outcome.status == DELIVERED else None,
                    updated_at=now,
                )
            )


async def _prune(
    sessions: async_sessionmaker[AsyncSession], settings: WebhookSettings, now: datetime
) -> int:
    before = now - timedelta(seconds=settings.retention_s)
    d = WEBHOOK_DELIVERIES
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        result = await session.execute(
            delete(d)
            .where(d.c.status.in_([DELIVERED, DEAD]), d.c.updated_at < before)
            .returning(d.c.id)
        )
        pruned = len(result.all())
        pruned += await OutboxRepository(session).prune_delivered(before)
    return pruned


async def deliver_pending(
    sessions: async_sessionmaker[AsyncSession],
    settings: WebhookSettings,
    send: Sender,
    *,
    writes: WriteSettings,
    clock: Clock = utc_now,
    resolve: Resolver = resolve_host,
) -> DeliveryReport:
    """One delivery pass (see module doc); call it periodically."""
    report = DeliveryReport()
    await _fan_out(sessions, settings, writes, clock(), report)
    claimed = await _claim(sessions, settings, clock())
    outcomes = [await _attempt(c, settings, send, clock, resolve) for c in claimed]
    if outcomes:
        await _record(sessions, outcomes, settings, clock())
    report.attempted = len(outcomes)
    report.delivered = sum(o.status == DELIVERED for o in outcomes)
    report.dead = sum(o.status == DEAD for o in outcomes)
    report.retrying = sum(o.status == PENDING for o in outcomes)
    for outcome in outcomes:
        if outcome.status != DELIVERED:
            logger.warning(
                "webhook delivery failed",
                extra={
                    "delivery": outcome.claimed.id,
                    "domain": outcome.claimed.domain_id,
                    "status": outcome.status,
                    "error": outcome.error,
                },
            )
    report.pruned = await _prune(sessions, settings, clock())
    return report
