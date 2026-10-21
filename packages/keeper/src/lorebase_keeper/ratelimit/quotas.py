"""Daily quotas of API keys: requests and context-pack tokens per UTC day.

Counting is done in memory and written to `api_key_usage` at most every
`quota.flush_interval_s` per key. A write adds this process's use to the stored row and returns
the total of every process, so several Keeper processes converge on the same figure and a quota
holds across restarts. Between writes a process enforces the quota on what it knows (the
stored total as of its last write, plus its own use since), which lets a key exceed a quota by
about one interval of traffic. A store failure never refuses a request: the quota is enforced
on what is known and the write is retried.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from typing import Literal

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider

from lorebase_keeper.auth.ports import AuthStore
from lorebase_keeper.ratelimit.config import QuotaSettings

logger = logging.getLogger("lorebase.keeper.quota")

QuotaKind = Literal["requests", "context_tokens"]


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """What a key has used today and may still use. A limit of `None` is no limit."""

    day: date
    resets_at: datetime
    requests_used: int
    requests_limit: int | None
    tokens_used: int
    tokens_limit: int | None

    def exceeded(self) -> QuotaKind | None:
        """The quota that is used up, if any."""
        if self.requests_limit is not None and self.requests_used >= self.requests_limit:
            return "requests"
        if self.tokens_limit is not None and self.tokens_used >= self.tokens_limit:
            return "context_tokens"
        return None


@dataclass
class _Entry:
    day: date
    stored_requests: int = 0
    stored_tokens: int = 0
    pending_requests: int = 0
    pending_tokens: int = 0
    loaded: bool = False
    flushing: bool = False
    last_flush: float = field(default=float("-inf"))


class QuotaTracker:
    """Counts the use of API keys against `QuotaSettings`."""

    def __init__(
        self,
        settings: QuotaSettings,
        store: AuthStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._monotonic = monotonic
        self._entries: dict[str, _Entry] = {}
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("lorebase.keeper")
        self._exceeded = meter.create_counter(
            "lorebase.http.quota_exceeded",
            unit="{request}",
            description="Requests refused with 429 because a daily quota was used up.",
        )

    # Use --------------------------------------------------------------------------------

    async def admit(self, key_id: str) -> tuple[QuotaKind | None, QuotaSnapshot]:
        """Count one request of the key unless a quota is used up.

        Returns the quota that refuses the request (nothing is counted then) and the state of
        the key.
        """
        entry = await self._entry(key_id)
        snapshot = self._snapshot(key_id, entry)
        refused = snapshot.exceeded()
        if refused is not None:
            self._exceeded.add(1, {"quota": refused})
            return refused, snapshot
        entry.pending_requests += 1
        await self._flush_when_due(key_id, entry)
        return None, self._snapshot(key_id, entry)

    async def check_tokens(self, key_id: str) -> QuotaSnapshot | None:
        """The key's state when its token quota is used up, otherwise `None`."""
        entry = await self._entry(key_id)
        snapshot = self._snapshot(key_id, entry)
        if snapshot.tokens_limit is not None and snapshot.tokens_used >= snapshot.tokens_limit:
            self._exceeded.add(1, {"quota": "context_tokens"})
            return snapshot
        return None

    async def charge_tokens(self, key_id: str, tokens: int) -> None:
        """Count context-pack tokens delivered to the key."""
        if tokens <= 0:
            return
        entry = await self._entry(key_id)
        entry.pending_tokens += tokens
        await self._flush_when_due(key_id, entry)

    async def snapshot(self, key_id: str) -> QuotaSnapshot:
        """The key's use today, current across processes."""
        entry = await self._entry(key_id)
        await self._flush(key_id, entry)
        return self._snapshot(key_id, entry)

    async def aclose(self) -> None:
        """Write what has not been written yet."""
        for key_id, entry in list(self._entries.items()):
            await self._flush(key_id, entry)

    # Internals --------------------------------------------------------------------------

    def _today(self) -> date:
        return self._clock().astimezone(UTC).date()

    def _snapshot(self, key_id: str, entry: _Entry) -> QuotaSnapshot:
        return QuotaSnapshot(
            day=entry.day,
            resets_at=datetime.combine(entry.day + timedelta(days=1), day_time.min, tzinfo=UTC),
            requests_used=entry.stored_requests + entry.pending_requests,
            requests_limit=self._settings.requests_limit(key_id),
            tokens_used=entry.stored_tokens + entry.pending_tokens,
            tokens_limit=self._settings.tokens_limit(key_id),
        )

    async def _entry(self, key_id: str) -> _Entry:
        """The key's counters for today, read from the store the first time."""
        today = self._today()
        entry = self._entries.get(key_id)
        if entry is not None and entry.day != today:
            await self._flush(key_id, entry)  # what the old day counted goes to the old day
            entry = None
        if entry is None:
            entry = self._entries[key_id] = _Entry(day=today)
        if not entry.loaded:
            await self._refresh(key_id, entry)
        return entry

    async def _refresh(self, key_id: str, entry: _Entry) -> None:
        try:
            async with self._store.unit() as unit:
                usage = await unit.usage.get(key_id, entry.day)
        except Exception:
            logger.warning("cannot read the usage of an api key", exc_info=True)
            return
        entry.stored_requests, entry.stored_tokens = usage.requests, usage.context_tokens
        entry.loaded = True

    async def _flush_when_due(self, key_id: str, entry: _Entry) -> None:
        if self._monotonic() - entry.last_flush >= self._settings.flush_interval_s:
            await self._flush(key_id, entry)

    async def _flush(self, key_id: str, entry: _Entry) -> None:
        """Add the pending use to the stored row and learn the total of all processes."""
        if entry.flushing:
            return
        entry.flushing = True
        entry.last_flush = self._monotonic()
        requests, tokens = entry.pending_requests, entry.pending_tokens
        try:
            async with self._store.unit() as unit:
                if requests or tokens:
                    total = await unit.usage.add(
                        key_id,
                        entry.day,
                        requests=requests,
                        context_tokens=tokens,
                        at=self._clock(),
                    )
                else:
                    total = await unit.usage.get(key_id, entry.day)
        except Exception:
            logger.warning("cannot write the usage of an api key", exc_info=True)
            return
        finally:
            entry.flushing = False
        entry.pending_requests -= requests
        entry.pending_tokens -= tokens
        entry.stored_requests, entry.stored_tokens = total.requests, total.context_tokens
        entry.loaded = True
