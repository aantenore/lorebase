"""Read replica support that needs no server: settings, readiness and the 503 without a database."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from lorebase_keeper.context import ReadSessionDep
from lorebase_keeper.readiness import CheckStatus, ReplicaCheck
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.replicas import ReplicaState

DSN = "postgresql+asyncpg://lorebase:s3cr3t-pass@db.internal:5432/lorebase"
REPLICA_DSN = "postgresql+asyncpg://lorebase:s3cr3t-pass@replica.internal:5432/lorebase"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


class FakeReplicas:
    def __init__(self, *states: ReplicaState) -> None:
        self.states = list(states)

    async def probe(self, timeout_s: float) -> list[ReplicaState]:
        return self.states


def state(name: str, lag: float | None = 10, *, reachable: bool = True) -> ReplicaState:
    return ReplicaState(
        name,
        f"{name}.internal:5432",
        reachable=reachable,
        in_recovery=True if reachable else None,
        lag_ms=lag if reachable else None,
        error=None if reachable else "OSError",
    )


def check(*states: ReplicaState, required: bool = False) -> ReplicaCheck:
    return ReplicaCheck(FakeReplicas(*states), timeout_s=1, max_lag_ms=1000, required=required)


def test_replicas_are_not_preferred_by_default() -> None:
    assert KeeperSettings().reads.prefer_replica is False


async def test_all_replicas_healthy() -> None:
    result = await check(state("replica-0"), state("replica-1", 250))()
    assert result.status is CheckStatus.OK
    assert result.detail == (
        "replica-0 (replica-0.internal:5432): ok, lag 10 ms; "
        "replica-1 (replica-1.internal:5432): ok, lag 250 ms"
    )


async def test_an_unhealthy_replica_degrades_without_failing() -> None:
    result = await check(state("replica-0"), state("replica-1", reachable=False))()
    assert result.status is CheckStatus.DEGRADED
    assert result.passed
    assert "replica-1 (replica-1.internal:5432): unreachable (OSError)" in (result.detail or "")


@pytest.mark.parametrize(
    ("lag", "text"), [(5000, "lagging (5000 ms)"), (None, "lagging (unknown)")]
)
async def test_a_lagging_replica_degrades(lag: float | None, text: str) -> None:
    result = await check(state("replica-0", lag))()
    assert result.status is CheckStatus.DEGRADED
    assert text in (result.detail or "")


async def test_no_usable_replica_fails_only_when_replicas_are_required() -> None:
    down = state("replica-0", reachable=False)
    assert (await check(down)()).status is CheckStatus.DEGRADED
    required = await check(down, required=True)()
    assert required.status is CheckStatus.FAILED
    assert not required.passed
    assert (await check(down, state("replica-1"), required=True)()).status is CheckStatus.DEGRADED


def test_replica_check_exists_only_with_replicas(make_app: Callable[..., FastAPI]) -> None:
    plain = make_app(sections={"database": {"dsn": DSN}})
    assert [c.name for c in plain.state.readiness_checks] == [
        "database",
        "vector_search",
    ]
    replicated = make_app(sections={"database": {"dsn": DSN, "read_dsn": [REPLICA_DSN]}})
    assert [c.name for c in replicated.state.readiness_checks] == [
        "database",
        "database_replicas",
        "vector_search",
    ]


def test_replica_check_is_required_without_fallback(make_app: Callable[..., FastAPI]) -> None:
    database = {"dsn": DSN, "read_dsn": [REPLICA_DSN], "read_fallback_to_primary": False}
    optional = make_app(sections={"database": database})
    required = make_app({"reads": {"prefer_replica": True}}, sections={"database": database})
    assert optional.state.readiness_checks[1]._required is False
    assert required.state.readiness_checks[1]._required is True


def test_injected_checks_are_not_extended(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(
        sections={"database": {"dsn": DSN, "read_dsn": [REPLICA_DSN]}},
        readiness_checks=[check(state("replica-0"))],
    )
    assert [c.name for c in app.state.readiness_checks] == ["database_replicas"]


async def test_health_report_carries_the_degraded_status(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app(readiness_checks=[check(state("replica-0", reachable=False))])
    async with client_for(app) as http:
        response = await http.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"]["database_replicas"]["status"] == "degraded"


def with_probe_route(app: FastAPI) -> FastAPI:
    router = APIRouter()

    @router.get("/probe/{domain_id}")
    async def probe(read: ReadSessionDep) -> dict[str, str]:
        return {"source": read.route.source.value}

    app.include_router(router)
    return app


async def test_read_session_without_a_database_is_unavailable(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(with_probe_route(make_app())) as http:
        response = await http.get("/probe/supply")
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


async def test_min_revision_must_be_a_revision_id(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = with_probe_route(make_app(sections={"database": {"dsn": DSN}}))
    async with client_for(app) as http:
        response = await http.get("/probe/supply", params={"min_revision": "xyz"})
    assert response.status_code == 422
