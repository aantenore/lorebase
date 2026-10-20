"""Read replicas: round-robin routing of reads behind a freshness guard.

Design 6.5, 6.6, 8.1 and 15.3. Writes always go to the primary (commits are serialized per
branch by an advisory lock, which only exists there). Reads may go to a streaming replica, which
trails the primary by a small and variable delay. The guard keeps that delay from being
observable where it matters:

- A read that must see a given branch head (`expected_head`, or the head read from the primary
  when `read_your_writes` is requested) runs a probe on the replica first. The probe reads the
  `refs` row of the branch in the same transaction the read will use. If the row does not hold
  that head the replica has not replayed the commit yet and the read goes to the next replica or,
  at last, to the primary. A commit is one transaction (design 6.6), so a replica showing the
  head shows everything the commit wrote. The check is an equality: when the branch has moved
  on since, the read is conservatively served by the primary.
- Every probe also reads the replication lag. A replica above `database.read_max_lag_ms` serves
  nothing, pinned or not.
- With `database.read_fallback_to_primary` off, a read no replica can serve raises
  `ReplicaUnavailableError` instead of loading the primary.

Replay lag is estimated like common monitoring tools do: zero when the standby has replayed all
WAL it received, otherwise the age of the last replayed transaction
(`pg_last_xact_replay_timestamp`, so primary and standby clocks should agree). It is advisory:
correctness of pinned reads rests on the head check, not on the lag figure.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.db import create_engine
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.settings import DatabaseSettings

logger = logging.getLogger("lorebase.store.replicas")

STATE_COLUMNS = """
    pg_is_in_recovery() AS in_recovery,
    CASE
        WHEN NOT pg_is_in_recovery() THEN 0
        WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() THEN 0
        ELSE extract(epoch FROM now() - pg_last_xact_replay_timestamp()) * 1000
    END::double precision AS lag_ms,
    pg_last_wal_replay_lsn()::text AS replay_lsn
"""
"""Columns describing a server's replication state; `lag_ms` is null when it cannot be known."""

STATE_SQL = f"SELECT {STATE_COLUMNS}"

_HEAD_OF_BRANCH = """
    (SELECT r.head FROM refs r
      WHERE r.domain_id = :domain_id
        AND r.name = coalesce(
            CAST(:branch AS text),
            (SELECT d.default_branch FROM domains d WHERE d.id = :domain_id)
        )) AS head
"""
_GUARD_SQL = text(f"SELECT {STATE_COLUMNS}, {_HEAD_OF_BRANCH}")
_PRIMARY_HEAD_SQL = text(f"SELECT {_HEAD_OF_BRANCH}")

_PROBE_ERRORS = (SQLAlchemyError, OSError, TimeoutError)


class ReadSource(StrEnum):
    """Which server served a read."""

    PRIMARY = "primary"
    REPLICA = "replica"


class RouteReason(StrEnum):
    """Why a read went where it did."""

    REPLICA_FRESH = "replica_fresh"
    NO_REPLICAS = "no_replicas"
    NOT_PREFERRED = "not_preferred"
    STALE = "stale"
    LAGGING = "lagging"
    UNREACHABLE = "unreachable"


class ReplicaUnavailableError(Exception):
    """No replica could serve the read and falling back to the primary is disabled."""

    def __init__(self, reason: RouteReason) -> None:
        super().__init__(f"no read replica can serve this read ({reason.value})")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReadRoute:
    """The routing decision of one read."""

    source: ReadSource
    reason: RouteReason
    replica: str | None = None
    """Name of the serving replica, or of the last one tried when a fallback happened."""
    lag_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ReadSession:
    """A session for reading and how it was chosen. The caller owns neither commits nor writes."""

    session: AsyncSession
    route: ReadRoute


@dataclass(frozen=True, slots=True)
class ReplicaState:
    """Replication state of one replica as last probed."""

    name: str
    endpoint: str
    """`host:port` of the replica; never contains credentials."""
    reachable: bool
    in_recovery: bool | None = None
    """False: the configured server is not a standby (a primary serves reads correctly)."""
    lag_ms: float | None = None
    replay_lsn: str | None = None
    error: str | None = None
    """Exception class name when the replica could not be probed."""

    def lagging(self, max_lag_ms: int) -> bool:
        """The lag is unknown or above the limit (a limit of 0 disables the check)."""
        return max_lag_ms > 0 and (self.lag_ms is None or self.lag_ms > max_lag_ms)

    def usable(self, max_lag_ms: int) -> bool:
        """The replica is reachable and within the lag limit."""
        return self.reachable and not self.lagging(max_lag_ms)


@dataclass(frozen=True, slots=True)
class Replica:
    """One replica: its pool and a label for logs and metrics."""

    name: str
    endpoint: str
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


def _endpoint(url: str) -> str:
    parsed = make_url(url)
    return f"{parsed.host or 'localhost'}:{parsed.port or 5432}"


class ReplicaSet:
    """Replicas in round-robin order, with the state each was last seen in."""

    def __init__(self, replicas: Sequence[Replica]) -> None:
        if not replicas:
            raise ValueError("a replica set needs at least one replica")
        self._replicas = tuple(replicas)
        self._cursor = itertools.count()
        self._states: dict[str, ReplicaState] = {}

    @classmethod
    def from_settings(cls, database: DatabaseSettings) -> ReplicaSet | None:
        """Replicas named by `database.read_dsn`; `None` when none is configured."""
        replicas = []
        for index, dsn in enumerate(database.read_dsn):
            engine = create_engine(database, url=dsn.get_secret_value())
            replicas.append(
                Replica(
                    name=f"replica-{index}",
                    endpoint=_endpoint(dsn.get_secret_value()),
                    engine=engine,
                    sessions=async_sessionmaker(engine, expire_on_commit=False),
                )
            )
        return cls(replicas) if replicas else None

    def __len__(self) -> int:
        return len(self._replicas)

    @property
    def replicas(self) -> tuple[Replica, ...]:
        """Every replica, in configuration order."""
        return self._replicas

    def rotation(self) -> list[Replica]:
        """Every replica once, starting at the next one in round-robin order."""
        start = next(self._cursor) % len(self._replicas)
        return [*self._replicas[start:], *self._replicas[:start]]

    def last_states(self) -> list[ReplicaState]:
        """State of each replica at its latest probe (reads probe too); unprobed ones are
        reported as not reachable."""
        return [
            self._states.get(r.name) or ReplicaState(r.name, r.endpoint, reachable=False)
            for r in self._replicas
        ]

    def record(self, state: ReplicaState) -> None:
        """Remember a probe result; logs when a replica becomes unreachable or recovers."""
        previous = self._states.get(state.name)
        if previous is not None and previous.reachable != state.reachable:
            if state.reachable:
                logger.info("read replica %s (%s) is reachable again", state.name, state.endpoint)
            else:
                logger.warning(
                    "read replica %s (%s) is unreachable: %s",
                    state.name,
                    state.endpoint,
                    state.error,
                )
        self._states[state.name] = state

    async def probe(self, timeout_s: float) -> list[ReplicaState]:
        """Probe every replica concurrently, each within `timeout_s`."""
        states = await asyncio.gather(*(_probe_state(r, timeout_s) for r in self._replicas))
        for state in states:
            self.record(state)
        return list(states)

    async def dispose(self) -> None:
        """Close every pool."""
        await asyncio.gather(*(r.engine.dispose() for r in self._replicas))


async def _probe_state(replica: Replica, timeout_s: float) -> ReplicaState:
    try:
        async with asyncio.timeout(timeout_s), replica.sessions() as session:
            row = (await session.execute(text(STATE_SQL))).one()
    except _PROBE_ERRORS as exc:
        return ReplicaState(
            replica.name, replica.endpoint, reachable=False, error=type(exc).__name__
        )
    return ReplicaState(
        replica.name,
        replica.endpoint,
        reachable=True,
        in_recovery=row.in_recovery,
        lag_ms=row.lag_ms,
        replay_lsn=row.replay_lsn,
    )


RouteObserver = Callable[[ReadRoute], None]


class ReadSessionFactory:
    """Opens read sessions on a replica when it is fresh enough, otherwise on the primary."""

    def __init__(
        self,
        primary: async_sessionmaker[AsyncSession],
        replicas: ReplicaSet | None = None,
        *,
        max_lag_ms: int = 0,
        fallback_to_primary: bool = True,
        probe_timeout_s: float = 2.0,
        on_route: RouteObserver | None = None,
    ) -> None:
        self._primary = primary
        self._replicas = replicas
        self._max_lag_ms = max_lag_ms
        self._fallback = fallback_to_primary
        self._probe_timeout_s = probe_timeout_s
        self._on_route = on_route

    @classmethod
    def from_settings(
        cls,
        primary: async_sessionmaker[AsyncSession],
        database: DatabaseSettings,
        *,
        on_route: RouteObserver | None = None,
    ) -> ReadSessionFactory:
        """Factory for the `database` settings; it reads from the primary alone when no
        replica is configured."""
        return cls(
            primary,
            ReplicaSet.from_settings(database),
            max_lag_ms=database.read_max_lag_ms,
            fallback_to_primary=database.read_fallback_to_primary,
            probe_timeout_s=database.read_probe_timeout_ms / 1000,
            on_route=on_route,
        )

    @property
    def replicas(self) -> ReplicaSet | None:
        """The replicas, `None` when reads always use the primary."""
        return self._replicas

    @property
    def max_lag_ms(self) -> int:
        """Lag above which a replica serves no reads (0: not checked)."""
        return self._max_lag_ms

    @property
    def fallback_to_primary(self) -> bool:
        return self._fallback

    @property
    def probe_timeout_s(self) -> float:
        return self._probe_timeout_s

    async def dispose(self) -> None:
        """Close the replica pools (the primary's belongs to its owner)."""
        if self._replicas is not None:
            await self._replicas.dispose()

    @asynccontextmanager
    async def open(
        self,
        *,
        domain_id: str | None = None,
        branch: str | None = None,
        expected_head: bytes | None = None,
        read_your_writes: bool = False,
        prefer_replica: bool = True,
    ) -> AsyncIterator[ReadSession]:
        """A session to read from, closed (rolled back) on exit.

        `domain_id` and `branch` (default: the Domain's default branch) identify the ref the read
        depends on. `expected_head` is a Revision id the branch must be at on the replica; with
        `read_your_writes` and no `expected_head`, the current head is read from the primary.
        `prefer_replica=False` reads from the primary. The session already has an open
        transaction scoped to `domain_id`.

        Raises `ReplicaUnavailableError` when no replica can serve the read and falling back is
        disabled.
        """
        if expected_head is not None and domain_id is None:
            raise ValueError("expected_head needs a domain_id")
        if self._replicas is None:
            session, route = self._primary(), ReadRoute(ReadSource.PRIMARY, RouteReason.NO_REPLICAS)
        elif not prefer_replica:
            session, route = (
                self._primary(),
                ReadRoute(ReadSource.PRIMARY, RouteReason.NOT_PREFERRED),
            )
        else:
            if expected_head is None and read_your_writes and domain_id is not None:
                expected_head = await self._primary_head(domain_id, branch)
            session, route = await self._choose(self._replicas, domain_id, branch, expected_head)
        if self._on_route is not None:
            self._on_route(route)
        try:
            if route.source is ReadSource.PRIMARY and domain_id is not None:
                await scope_to_domain(session, domain_id)
            yield ReadSession(session, route)
        finally:
            await session.close()

    async def _primary_head(self, domain_id: str, branch: str | None) -> bytes | None:
        async with self._primary() as session:
            await scope_to_domain(session, domain_id)
            result = await session.execute(
                _PRIMARY_HEAD_SQL, {"domain_id": domain_id, "branch": branch}
            )
            head: bytes | None = result.scalar_one_or_none()
            return head

    async def _choose(
        self,
        replicas: ReplicaSet,
        domain_id: str | None,
        branch: str | None,
        expected_head: bytes | None,
    ) -> tuple[AsyncSession, ReadRoute]:
        last = RouteReason.UNREACHABLE
        last_replica: str | None = None
        last_lag: float | None = None
        for replica in replicas.rotation():
            session = replica.sessions()
            try:
                async with asyncio.timeout(self._probe_timeout_s):
                    state, head = await self._probe(replica, session, domain_id, branch)
            except _PROBE_ERRORS as exc:
                await _close_quietly(session)
                replicas.record(
                    ReplicaState(
                        replica.name, replica.endpoint, reachable=False, error=type(exc).__name__
                    )
                )
                last, last_replica, last_lag = RouteReason.UNREACHABLE, replica.name, None
                continue
            replicas.record(state)
            if state.lagging(self._max_lag_ms):
                reason = RouteReason.LAGGING
            elif expected_head is not None and head != expected_head:
                reason = RouteReason.STALE
            else:
                route = ReadRoute(
                    ReadSource.REPLICA, RouteReason.REPLICA_FRESH, replica.name, state.lag_ms
                )
                return session, route
            await _close_quietly(session)
            last, last_replica, last_lag = reason, replica.name, state.lag_ms
        if not self._fallback:
            raise ReplicaUnavailableError(last)
        return self._primary(), ReadRoute(ReadSource.PRIMARY, last, last_replica, last_lag)

    @staticmethod
    async def _probe(
        replica: Replica, session: AsyncSession, domain_id: str | None, branch: str | None
    ) -> tuple[ReplicaState, bytes | None]:
        if domain_id is None:
            row = (await session.execute(text(STATE_SQL))).one()
            head = None
        else:
            await scope_to_domain(session, domain_id)
            row = (
                await session.execute(_GUARD_SQL, {"domain_id": domain_id, "branch": branch})
            ).one()
            head = row.head
        state = ReplicaState(
            replica.name,
            replica.endpoint,
            reachable=True,
            in_recovery=row.in_recovery,
            lag_ms=row.lag_ms,
            replay_lsn=row.replay_lsn,
        )
        return state, head


async def _close_quietly(session: AsyncSession) -> None:
    try:
        await asyncio.shield(session.close())
    except Exception:
        logger.debug("closing a replica session failed", exc_info=True)
