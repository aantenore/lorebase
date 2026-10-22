"""Per-application state of the read path and the read transaction dependency.

Reads run in a read-only transaction with the `read.statement_timeout_ms` statement timeout.
The Domain is added to the row-level security scope when the read point is resolved
(`reading.points`), so every query of a request stays inside its Domain.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from threading import Lock
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_keeper.errors import UNAVAILABLE, ProblemError
from lorebase_keeper.reading.caching import ResponseCache, build_cache
from lorebase_keeper.reading.settings import (
    ReadSettings,
    SearchSettings,
    read_settings,
    search_settings,
)
from lorebase_keeper.settings import SensitivitySettings, keeper_settings
from lorebase_platform.settings import Settings
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings
from lorebase_store.vault import VaultKeys

_STATE_KEY = "read_runtime"


class SchemaCache:
    """Thread-safe LRU of compiled schemas keyed by `(domain_id, schema partition hash)`."""

    def __init__(self, max_entries: int) -> None:
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, bytes], Schema] = OrderedDict()
        self._lock = Lock()

    def get(self, domain_id: str, schema_hash: bytes) -> Schema | None:
        key = (domain_id, schema_hash)
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
            return found

    def put(self, domain_id: str, schema_hash: bytes, schema: Schema) -> None:
        with self._lock:
            self._entries[(domain_id, schema_hash)] = schema
            self._entries.move_to_end((domain_id, schema_hash))
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)


@dataclass
class ReadRuntime:
    """Settings, sessions and caches shared by the reads of one application."""

    sessions: async_sessionmaker[AsyncSession]
    read: ReadSettings
    search: SearchSettings
    store: StoreRuntime
    cache: ResponseCache | None = None
    sensitivity: SensitivitySettings = field(default_factory=SensitivitySettings)
    schemas: SchemaCache = field(init=False)

    def __post_init__(self) -> None:
        self.schemas = SchemaCache(self.read.schema_cache_entries)

    @property
    def store_settings(self) -> StoreSettings:
        return self.store.settings

    @property
    def vault(self) -> VaultKeys | None:
        """Keys of the personal data vault; None when it is not configured."""
        return self.store.vault

    @classmethod
    def build(cls, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> ReadRuntime:
        reads = keeper_settings(settings).reads
        return cls(
            sessions=sessions,
            read=read_settings(settings),
            search=search_settings(settings),
            store=StoreRuntime.from_loaded(settings),
            cache=build_cache(reads.cache),
            sensitivity=reads.sensitivity,
        )


def get_read_runtime(request: Request) -> ReadRuntime:
    """The read runtime of the application, built on first use; 503 without a database."""
    state = request.app.state
    runtime = getattr(state, _STATE_KEY, None)
    if runtime is not None:
        return cast(ReadRuntime, runtime)
    database = getattr(state, "database", None)
    if database is None:
        raise ProblemError(UNAVAILABLE, "Reads need a configured database.")
    runtime = ReadRuntime.build(cast(Settings, state.settings), database.sessions)
    setattr(state, _STATE_KEY, runtime)
    return runtime


ReadRuntimeDep = Annotated[ReadRuntime, Depends(get_read_runtime)]


async def begin_read(session: AsyncSession, runtime: ReadRuntime) -> None:
    """Make the session's (new) transaction read-only with the read statement timeout."""
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(
        select(func.set_config("statement_timeout", str(runtime.read.statement_timeout_ms), True))
    )


async def read_session(runtime: ReadRuntimeDep) -> AsyncIterator[AsyncSession]:
    """A session in a read-only transaction, closed after the response."""
    async with runtime.sessions() as session, session.begin():
        await begin_read(session, runtime)
        yield session


ReadSessionDep = Annotated[AsyncSession, Depends(read_session)]
