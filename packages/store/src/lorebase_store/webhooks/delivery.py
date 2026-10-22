"""Webhook delivery from the outbox (design 10.5): `deliver_pending`, one worker pass.

A pass, safe to run concurrently in several workers (rows are claimed with `SKIP LOCKED`):

1. fan out: the oldest outbox events pending for the `webhooks` consumer become one delivery
   per matching webhook (event type or `operation.*`, branch, created before the event), and
   stop being pending for it, in one transaction (`repositories.events`: other consumers of the
   outbox keep their own pending rows);
2. claim the due deliveries, leasing them for `lease_s`;
3. send each one as an HMAC-signed JSON POST (`webhooks.signing`) through the injected
   `Sender`, up to `concurrency` at a time. Each attempt, host lookup included, is cut off
   after `timeout_s`, so a target that accepts and never answers, drops packets or closes
   midway fails its attempt instead of holding the pass. No attempt starts after
   `pass_timeout_s`: the deliveries left are released to the next pass without counting an
   attempt. A 2xx answer delivers a delivery, anything else is retried after
   `min(backoff_base_s * backoff_factor ** (attempts - 1), backoff_max_s)` until
   `max_attempts`, then marked dead. Targets outside `allowed_schemes`, or resolving to
   private addresses unless `allow_private_targets`, are marked dead at once. The host is
   resolved once per attempt and the `Sender` connects to those checked addresses only, so a
   second lookup (DNS rebinding) cannot redirect the request to an internal service. The body
   is rendered for the webhook when it is claimed (`outbox_events.disclose`): sensitive values
   are masked unless the webhook is cleared for their tags, vault values are opened then.
   Every attempt of an event carries the same `idempotency_header` (`evt_<sequence>`):
   delivery is at least once (a pass that dies after sending and before recording sends
   again), so receivers drop repeats by it. Deliveries are not ordered: retries interleave
   with newer events; payloads carry `sequence` (the outbox id) for receivers that need one;
4. prune finished deliveries older than `retention_s`, and outbox events by `store.outbox`
   (`OutboxRepository.prune`).

Before these steps, with `encryption_key` set, up to `batch` signing keys stored usable (or
under a previous encryption key) are sealed under it (`webhooks.keys`). A delivery whose key
cannot be opened (sealed under a key that is no longer configured) fails its attempt with an
error naming the setting.

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
from lorebase_store.outbox_events import cleared_tokens, disclose, subscribed
from lorebase_store.repositories.events import OutboxRepository, OutboxRow
from lorebase_store.settings import OutboxSettings, WriteSettings
from lorebase_store.telemetry import (
    OUTBOX_HANDOFFS,
    OUTBOX_PRUNED,
    WEBHOOK_OUTCOMES,
    WEBHOOK_TIMEOUTS,
)
from lorebase_store.vault import VaultKeys, VaultValue, open_values
from lorebase_store.webhooks.keys import SigningKeys, StoredKey, seal_stored_keys
from lorebase_store.webhooks.repository import WEBHOOK_DELIVERIES, WEBHOOKS, webhook_row
from lorebase_store.webhooks.settings import WebhookSettings
from lorebase_store.webhooks.signing import signature

logger = logging.getLogger("lorebase.store.webhooks")

WEBHOOKS_CONSUMER = "webhooks"
"""The outbox consumer of webhook delivery (registered by revision 0020)."""
PENDING, DELIVERED, DEAD = "pending", "delivered", "dead"
_OUTCOMES = {DELIVERED: "delivered", PENDING: "retrying", DEAD: "dead"}
"""Metric outcome of an attempt by the status it leaves the delivery in."""
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


class TargetPolicy(Protocol):
    """What the target checks read from settings (`WebhookSettings`, and the HTTP sinks of
    audit checkpoints)."""

    @property
    def allowed_schemes(self) -> tuple[str, ...]: ...

    @property
    def allow_private_targets(self) -> bool: ...


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


def scheme_problem(url: str, settings: TargetPolicy) -> str | None:
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


async def check_target(url: str, settings: TargetPolicy, resolve: Resolver) -> Target:
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


async def target_problem(url: str, settings: TargetPolicy, resolve: Resolver) -> str | None:
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
    sealed: int = 0
    timed_out: int = 0
    """Attempts cut off by `timeout_s` (counted in `retrying` or `dead` as well)."""
    released: int = 0
    """Claimed deliveries not attempted before `pass_timeout_s`, left to the next pass."""


@dataclass(frozen=True, slots=True)
class _Claimed:
    domain_id: str
    id: int
    url: str
    key: bytes | None
    """The signing key; None when it is sealed under a key that is not configured."""
    event_type: str
    payload: dict[str, Any]
    """The body as this webhook receives it (`disclose`d)."""
    attempts: int
    event_id: int = 0


@dataclass(frozen=True, slots=True)
class _Outcome:
    claimed: _Claimed
    status: str
    http_status: int | None
    error: str | None
    timed_out: bool = False


async def _payload(
    session: AsyncSession,
    event: OutboxRow,
    schemas: dict[tuple[str, str], Schema | None],
    settings: WebhookSettings,
    writes: WriteSettings,
) -> dict[str, Any]:
    if event.event_type != COMMITTED_EVENT:
        return {**event.payload, "type": event.event_type, "sequence": event.id}
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
    return {**event.payload, "type": event.event_type, "sequence": event.id}


async def _fan_out(
    sessions: async_sessionmaker[AsyncSession],
    settings: WebhookSettings,
    writes: WriteSettings,
    now: datetime,
    report: DeliveryReport,
) -> None:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        outbox = OutboxRepository(session)
        events = await outbox.claim(WEBHOOKS_CONSUMER, settings.batch)
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
                if subscribed(event.event_type, hook.event_types)
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
        await outbox.acknowledge(WEBHOOKS_CONSUMER, [e.id for e in events])
        report.events += len(events)
        report.queued += len(rows)
    OUTBOX_HANDOFFS.add(len(events), {"consumer": WEBHOOKS_CONSUMER})


async def _claim(
    sessions: async_sessionmaker[AsyncSession],
    settings: WebhookSettings,
    signing_keys: SigningKeys,
    now: datetime,
    vault: VaultKeys | None = None,
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
                w.c.sealed_key,
                w.c.clearances,
                d.c.event_type,
                d.c.event_id,
                d.c.payload,
                d.c.attempts,
            )
            .join(w, and_(w.c.domain_id == d.c.domain_id, w.c.id == d.c.webhook_id))
            .where(tuple_(d.c.domain_id, d.c.id).in_(keys))
            .order_by(d.c.id)
        )
        rows = list(result)
        values = await _vault_values(session, rows, vault)
        claimed = []
        for row in rows:
            opened = signing_keys.open(StoredKey(row.secret_hash, row.sealed_key))
            payload = disclose(
                row.payload,
                tuple(row.clearances or ()),
                values.get(row.domain_id, {}),
                masked=settings.masked_value,
                erased=settings.erased_value,
            )
            claimed.append(
                _Claimed(
                    row.domain_id,
                    row.id,
                    row.url,
                    None if opened is None else opened.key,
                    row.event_type,
                    payload,
                    row.attempts,
                    row.event_id,
                )
            )
        return claimed


async def _vault_values(
    session: AsyncSession, rows: Sequence[Any], vault: VaultKeys | None
) -> dict[str, dict[str, VaultValue]]:
    """Per Domain, the vault values the claimed deliveries disclose to their webhooks."""
    tokens: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        tokens[row.domain_id] |= cleared_tokens(row.payload, tuple(row.clearances or ()))
    return {
        domain_id: await open_values(session, domain_id, wanted, vault)
        for domain_id, wanted in tokens.items()
        if wanted
    }


async def _attempt(
    claimed: _Claimed,
    settings: WebhookSettings,
    send: Sender,
    clock: Clock,
    resolve: Resolver,
) -> _Outcome:
    if claimed.key is None:
        return _Outcome(
            claimed,
            _after_failure(claimed, settings),
            None,
            "the signing key cannot be opened: it was sealed under a key that is neither "
            "webhooks.encryption_key nor in webhooks.previous_encryption_keys",
        )
    body = json.dumps(claimed.payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        settings.signature_header: signature(claimed.key, int(clock().timestamp()), body),
        settings.event_header: claimed.event_type,
        settings.delivery_header: str(claimed.id),
        settings.idempotency_header: f"evt_{claimed.event_id}",
    }
    try:
        # One deadline for the whole attempt: the lookup and every address the sender tries.
        async with asyncio.timeout(settings.timeout_s):
            target = await check_target(claimed.url, settings, resolve)
            if target.problem is not None:
                return _Outcome(claimed, DEAD, None, f"target refused: {target.problem}")
            status = await send(claimed.url, body, headers, settings.timeout_s, target.addresses)
    except TimeoutError:
        message = f"timed out after {settings.timeout_s:g}s"
        return _Outcome(claimed, _after_failure(claimed, settings), None, message, timed_out=True)
    except Exception as error:  # any transport failure is retried
        message = f"{type(error).__name__}: {error}"[:ERROR_LENGTH]
        return _Outcome(claimed, _after_failure(claimed, settings), None, message)
    if 200 <= status < 300:
        return _Outcome(claimed, DELIVERED, status, None)
    return _Outcome(claimed, _after_failure(claimed, settings), status, f"HTTP {status}")


async def _attempt_all(
    claimed: Sequence[_Claimed],
    settings: WebhookSettings,
    send: Sender,
    clock: Clock,
    resolve: Resolver,
) -> tuple[list[_Outcome], list[_Claimed]]:
    """Attempt the claimed deliveries, `concurrency` at a time, starting none after
    `pass_timeout_s`; returns the outcomes and the deliveries not attempted."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.pass_timeout_s
    gate = asyncio.Semaphore(settings.concurrency)

    async def one(delivery: _Claimed) -> _Outcome | None:
        async with gate:
            if loop.time() >= deadline:
                return None
            return await _attempt(delivery, settings, send, clock, resolve)

    results = await asyncio.gather(*(one(delivery) for delivery in claimed))
    outcomes = [result for result in results if result is not None]
    left = [delivery for delivery, result in zip(claimed, results, strict=True) if result is None]
    return outcomes, left


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


async def _release(
    sessions: async_sessionmaker[AsyncSession], left: Sequence[_Claimed], now: datetime
) -> None:
    """Make claimed deliveries that were not attempted due again, ending their lease."""
    d = WEBHOOK_DELIVERIES
    keys = [(delivery.domain_id, delivery.id) for delivery in left]
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        await session.execute(
            update(d)
            .where(tuple_(d.c.domain_id, d.c.id).in_(keys), d.c.status == PENDING)
            .values(next_attempt_at=now)
        )


async def _prune(
    sessions: async_sessionmaker[AsyncSession],
    settings: WebhookSettings,
    outbox: OutboxSettings,
    now: datetime,
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
        handed, expired = await OutboxRepository(session).prune(
            before=now - timedelta(seconds=outbox.retention_s),
            expire_before=now - timedelta(seconds=outbox.max_age_s),
            limit=outbox.prune_batch,
        )
    if handed:
        OUTBOX_PRUNED.add(handed, {"reason": "retention"})
    if expired:
        OUTBOX_PRUNED.add(expired, {"reason": "expired"})
        logger.warning(
            "outbox events expired before every consumer handed them off",
            extra={"expired": expired, "max_age_s": outbox.max_age_s},
        )
    return pruned + handed + expired


async def deliver_pending(
    sessions: async_sessionmaker[AsyncSession],
    settings: WebhookSettings,
    send: Sender,
    *,
    writes: WriteSettings,
    outbox: OutboxSettings | None = None,
    vault: VaultKeys | None = None,
    clock: Clock = utc_now,
    resolve: Resolver = resolve_host,
) -> DeliveryReport:
    """One delivery pass (see module doc); call it periodically. `outbox` sets the retention
    of outbox events (default: code defaults); `vault` opens the vault values that webhooks
    cleared for them receive (without it they receive `masked_value`)."""
    report = DeliveryReport()
    keys = SigningKeys.from_settings(settings)
    if keys.sealing:
        async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
            report.sealed = await seal_stored_keys(session, keys, limit=settings.batch)
    await _fan_out(sessions, settings, writes, clock(), report)
    claimed = await _claim(sessions, settings, keys, clock(), vault)
    outcomes, left = await _attempt_all(claimed, settings, send, clock, resolve)
    if outcomes:
        await _record(sessions, outcomes, settings, clock())
    if left:
        await _release(sessions, left, clock())
        logger.warning(
            "webhook pass out of time",
            extra={"released": len(left), "pass_timeout_s": settings.pass_timeout_s},
        )
    report.attempted = len(outcomes)
    report.released = len(left)
    report.timed_out = sum(o.timed_out for o in outcomes)
    report.delivered = sum(o.status == DELIVERED for o in outcomes)
    report.dead = sum(o.status == DEAD for o in outcomes)
    report.retrying = sum(o.status == PENDING for o in outcomes)
    for outcome in outcomes:
        WEBHOOK_OUTCOMES.add(1, {"outcome": _OUTCOMES[outcome.status]})
        if outcome.timed_out:
            WEBHOOK_TIMEOUTS.add(1)
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
    report.pruned = await _prune(sessions, settings, outbox or OutboxSettings(), clock())
    return report
