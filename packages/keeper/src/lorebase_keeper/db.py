"""Database engine and auth store of the application, built from the `database` section."""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, MeterProvider, Observation
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_platform.settings import Settings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.replicas import ReadRoute, ReadSessionFactory, ReplicaSet
from lorebase_store.settings import DatabaseSettings

METER_NAME = "lorebase.keeper"


@dataclass(frozen=True)
class Database:
    """Engine, session factory and auth store sharing one connection pool.

    `reads` opens read sessions on a replica when one is configured and fresh enough, and on the
    primary otherwise.
    """

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    auth_store: SqlAuthStore
    reads: ReadSessionFactory | None = None

    async def dispose(self) -> None:
        """Close the pools."""
        if self.reads is not None:
            await self.reads.dispose()
        await self.engine.dispose()


def create_database(
    settings: Settings, section: str, *, meter_provider: MeterProvider | None = None
) -> Database | None:
    """The database named by `settings[section]`, or None when that section is absent.

    Connections are opened lazily, so building it does not require the server to be up. Read
    routing and replica lag metrics go to `meter_provider` (default: the global provider).
    """
    if not (settings.model_extra or {}).get(section):
        return None
    database = settings.section(section, DatabaseSettings)
    engine = create_engine(database)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    meter = (meter_provider or metrics.get_meter_provider()).get_meter(METER_NAME)
    routed = meter.create_counter(
        "lorebase.db.reads", description="Reads by serving database and routing reason."
    )

    def count(route: ReadRoute) -> None:
        routed.add(1, {"source": route.source.value, "reason": route.reason.value})

    reads = ReadSessionFactory.from_settings(sessions, database, on_route=count)
    if reads.replicas is not None:
        _observe_lag(meter, reads.replicas)
    return Database(
        engine=engine, sessions=sessions, auth_store=SqlAuthStore(sessions), reads=reads
    )


def _observe_lag(meter: metrics.Meter, replicas: ReplicaSet) -> None:
    def lag(_: CallbackOptions) -> list[Observation]:
        return [
            Observation(state.lag_ms, {"replica": state.name})
            for state in replicas.last_states()
            if state.lag_ms is not None
        ]

    def reachable(_: CallbackOptions) -> list[Observation]:
        return [
            Observation(int(state.reachable), {"replica": state.name})
            for state in replicas.last_states()
        ]

    meter.create_observable_gauge(
        "lorebase.db.replica.lag",
        callbacks=[lag],
        unit="ms",
        description="Replay lag of each read replica at its latest probe.",
    )
    meter.create_observable_gauge(
        "lorebase.db.replica.up",
        callbacks=[reachable],
        description="1 when the read replica answered its latest probe.",
    )
