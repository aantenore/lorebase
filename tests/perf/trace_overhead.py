"""Latency overhead of the Keeper agent trace (docs/agent-trace.md) on read requests.

The real Keeper application runs in process (ASGI transport, no socket) against the seeded
database, authentication mode `none`. Tracing is enabled in its settings and opt-in per request:
a request carrying the opt-in and session headers is traced (the "on" case), one without them is
not (the "off" case). The two are called in turn with the same random keys and timed client
side, so both see the same machine load. The response cache is disabled
(`keeper.reads.cache.enabled`), or repeated reads would answer from memory.

The trace is written off the request path; what the "on" case adds is capturing the query and
the response counts and queueing the step. After measuring, the application is shut down (which
flushes the queue) and the steps written are counted.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import harness
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_keeper.app import create_app
from lorebase_keeper.settings import KeeperSettings
from lorebase_platform.settings import Settings

#: A read as an HTTP request: the client, the base path of the Domain, the random generator and
#: the headers to send.
Request = Callable[
    [httpx.AsyncClient, str, random.Random, dict[str, str]], Awaitable[httpx.Response]
]

SESSION_PREFIX = "perf-"
OPT_IN_VALUE = "on"
NEIGHBORHOOD_DEPTH = 2
SEARCH_LIMIT = 20


@dataclass(frozen=True, slots=True)
class TraceOverhead:
    """`reads`: read name -> (trace off, trace on); `steps_written` of `steps_expected`
    (calls with the trace on, warmup included) found in `agent_trace_steps` after shutdown."""

    reads: dict[str, tuple[harness.Measurement, harness.Measurement]]
    steps_written: int
    steps_expected: int


def _requests(work: harness.Workload) -> dict[str, Request]:
    """The HTTP reads, named after the `harness.TARGETS_MS` entries they correspond to."""
    part = work.schema.kinds[work.part_kind].api_name

    async def get(
        http: httpx.AsyncClient, base: str, rng: random.Random, headers: dict[str, str]
    ) -> httpx.Response:
        return await http.get(
            f"{base}/kinds/{part}/records/{rng.choice(work.parts)}", headers=headers
        )

    async def search(
        http: httpx.AsyncClient, base: str, rng: random.Random, headers: dict[str, str]
    ) -> httpx.Response:
        body = {"q": rng.choice(work.words), "limit": SEARCH_LIMIT}
        return await http.post(f"{base}/search", json=body, headers=headers)

    async def hood(
        http: httpx.AsyncClient, base: str, rng: random.Random, headers: dict[str, str]
    ) -> httpx.Response:
        body = {
            "seeds": [{"kind": part, "key": rng.choice(work.parts)}],
            "depth": NEIGHBORHOOD_DEPTH,
        }
        return await http.post(f"{base}/neighborhood", json=body, headers=headers)

    return {"get by key": get, "full-text search": search, "2-hop neighborhood": hood}


async def measure_trace_overhead(
    url: str,
    work: harness.Workload,
    *,
    iterations: int,
    warmup: int,
    seed: int = 7,
) -> TraceOverhead:
    """p50/p95/p99 of each read with the trace off and on, called in turn with the same keys.

    `url` is the asyncpg URL of the seeded database; as the owner role it bypasses row-level
    security, so the figures leave RLS out of both cases alike.
    """
    await delete_steps(url)
    keeper = KeeperSettings()
    trace = keeper.trace.model_copy(update={"enabled": True})
    settings = Settings(
        keeper={"trace": trace.model_dump(), "reads": {"cache": {"enabled": False}}},
        database={"dsn": url},
    )
    app = create_app(settings)
    base = f"{keeper.api_prefix}/domains/{harness.DOMAIN}"
    requests = _requests(work)
    reads = {name: (harness.Measurement(name), harness.Measurement(name)) for name in requests}
    traced_calls = 0
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        for name, request in requests.items():
            off, on = reads[name]
            cases = ((False, random.Random(seed), off), (True, random.Random(seed), on))
            for index in range(warmup + iterations):
                session = f"{SESSION_PREFIX}{name.replace(' ', '-')}-{index}"
                traced = {trace.opt_in_header: OPT_IN_VALUE, trace.session_header: session}
                for opted_in, rng, measurement in cases:
                    headers = traced if opted_in else {}
                    started = time.perf_counter()
                    response = await request(http, base, rng, headers)
                    elapsed = (time.perf_counter() - started) * 1000
                    assert response.status_code == 200, (name, response.text)
                    if index >= warmup:
                        measurement.samples_ms.append(elapsed)
                traced_calls += 1
            harness._progress(f"trace off {harness._summary(off, None)}")
            harness._progress(f"trace on  {harness._summary(on, None)}")
    # Leaving the lifespan closed the recorder, which writes what was still queued.
    return TraceOverhead(reads, await _steps_written(url), traced_calls)


async def delete_steps(url: str) -> None:
    """Delete the steps of the sessions this module opens (left by an earlier run)."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM agent_trace_steps WHERE session_id LIKE :p"),
                {"p": f"{SESSION_PREFIX}%"},
            )
    finally:
        await engine.dispose()


async def _steps_written(url: str) -> int:
    """Steps of the sessions this module opened."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            found = await connection.execute(
                text("SELECT count(*) FROM agent_trace_steps WHERE session_id LIKE :p"),
                {"p": f"{SESSION_PREFIX}%"},
            )
            return int(found.scalar_one())
    finally:
        await engine.dispose()
