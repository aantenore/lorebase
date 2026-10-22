"""Audit of reads: filters, sampling, sensitive reads, batching and drops."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lorebase_keeper.auth.config import ReadAuditSettings
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.read_audit import (
    ReadAuditor,
    collecting,
    note_sensitive,
)
from lorebase_platform.settings import ConfigError
from lorebase_store.auth import NewAuditEntry
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
KEYS = "/api/v1/domains/acme/api-keys"
PEEK = "/api/v1/domains/{domain_id}/kinds/{kind}/peek"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


class Sink:
    def __init__(self) -> None:
        self.batches: list[list[NewAuditEntry]] = []
        self.fail = False
        self.attempts = 0

    async def __call__(self, entries: Sequence[NewAuditEntry]) -> None:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("database down")
        self.batches.append(list(entries))

    @property
    def entries(self) -> list[NewAuditEntry]:
        return [e for batch in self.batches for e in batch]


def auth(reads: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "api_key", "api_keys": {"pepper": PEPPER}, "audit": {"reads": reads}}


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def headers(store: Any, codec: Any) -> Callable[[str, PrincipalType], dict[str, str]]:
    def make(principal: str, type_: PrincipalType = PrincipalType.SERVICE) -> dict[str, str]:
        store.add_principal(principal, type_)
        store.add_grant("acme", principal, "admin")
        token, _ = store.add_key(codec, principal)
        return {"Authorization": f"Bearer {token}"}

    return make


def with_peek(app: FastAPI) -> FastAPI:
    """A read route that renders a sensitive value for Kind `customer` only."""
    router = APIRouter()

    @router.get(PEEK)
    async def peek(
        domain_id: str, kind: str, access: Annotated[Access, Depends(require(Permission.DATA_READ))]
    ) -> dict[str, str]:
        if kind == "customer":
            note_sensitive("masked")
        return {"kind": kind}

    app.include_router(router)
    return app


async def read_entries(app: FastAPI, sink: Sink) -> list[NewAuditEntry]:
    reads: ReadAuditor = app.state.read_audit
    await reads.flush()
    return sink.entries


async def test_reads_are_recorded_in_their_own_stream(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, sink: Sink, headers: Any
) -> None:
    app = auth_app(auth({"enabled": True}), read_audit_sink=sink)
    admin = headers("svc_admin")
    async with client_for(app) as http:
        response = await http.get(KEYS, headers=admin)
        assert response.status_code == 200
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.post(KEYS, json={"name": "bot"}, headers=admin)).status_code == 201
    [entry] = await read_entries(app, sink)
    assert (entry.actor, entry.action, entry.domain_id) == ("svc_admin", "read", "acme")
    assert entry.details["route"] == "/api/v1/domains/{domain_id}/api-keys"
    assert entry.details["principal_type"] == "service"
    assert entry.details["request_id"] == response.headers["x-request-id"]
    assert "sensitive" not in entry.details
    assert [e.action for e in store.audit_rows] == ["apikey.create"]  # actions stay apart


async def test_filters_and_sampling_never_skip_sensitive_reads(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, sink: Sink, headers: Any
) -> None:
    settings = {"enabled": True, "sample_rate": 0, "kinds": ["supplier"]}
    app = with_peek(auth_app(auth(settings), read_audit_sink=sink))
    admin = headers("svc_admin")
    async with client_for(app) as http:
        for kind in ("supplier", "customer", "order"):
            assert (
                await http.get(PEEK.format(domain_id="acme", kind=kind), headers=admin)
            ).status_code == 200
    [entry] = await read_entries(app, sink)
    assert entry.details["kind"] == entry.target == "customer"
    assert entry.details["sensitive"] == ["masked"]


async def test_principal_types_filter(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, sink: Sink, headers: Any
) -> None:
    app = with_peek(
        auth_app(auth({"enabled": True, "principal_types": ["user"]}), read_audit_sink=sink)
    )
    service, user = headers("svc_bot"), headers("usr_ann", PrincipalType.USER)
    async with client_for(app) as http:
        for who in (service, user):
            r = await http.get(PEEK.format(domain_id="acme", kind="order"), headers=who)
            assert r.status_code == 200, r.text
    assert [e.actor for e in await read_entries(app, sink)] == ["usr_ann"]


async def test_reads_need_a_database(auth_app: Callable[..., FastAPI]) -> None:
    with pytest.raises(ConfigError, match=r"audit\.reads"):
        auth_app(auth({"enabled": True}))


async def test_writes_are_batched_and_drops_are_counted() -> None:
    reader = InMemoryMetricReader()
    sink = Sink()
    settings = ReadAuditSettings(enabled=True, queue_size=3, batch_size=2, flush_interval_s=0.01)
    reads = ReadAuditor(settings, sink, meter_provider=MeterProvider(metric_readers=[reader]))
    plain = NewAuditEntry(actor="usr_1", action="read")
    sensitive = NewAuditEntry(actor="usr_1", action="read", details={"sensitive": ["shown"]})
    offered = [reads.offer(plain) for _ in range(3)]
    # nothing written yet; the last place is kept for sensitive reads
    assert offered == [True, True, False]
    assert reads.offer(sensitive)
    assert not reads.offer(sensitive)  # the queue is full
    await reads.flush()
    assert [len(b) for b in sink.batches] == [2, 1]
    sink.fail = True
    assert reads.offer(plain)
    assert reads.offer(sensitive)
    await reads.flush()
    await reads.aclose()
    dropped = {
        (point.attributes["reason"], point.attributes["sensitive"]): point.value
        for metric in reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
        if metric.name == "lorebase.audit.reads.dropped"
        for point in metric.data.data_points
    }
    assert dropped == {
        ("queue_full", False): 1,
        ("queue_full", True): 1,
        ("write_failed", False): 1,
        ("write_failed", True): 1,  # after its retry
    }
    assert sink.attempts == 4  # two batches, then one batch tried twice


async def test_notes_belong_to_their_request() -> None:
    note_sensitive("shown")  # outside a request: ignored

    async def request(outcome: str | None) -> set[str]:
        with collecting() as notes:
            await asyncio.sleep(0)
            if outcome:
                note_sensitive("shown")
            return notes.sensitive

    assert await asyncio.gather(request("shown"), request(None)) == [{"shown"}, set()]
