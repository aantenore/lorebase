"""Audit of reads: filters, sampling, sensitive reads, batching and drops."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import StreamingResponse
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lorebase_keeper.auth.config import ReadAuditSettings
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.read_audit import (
    ReadAuditCheck,
    ReadAuditor,
    collecting,
    note_sensitive,
)
from lorebase_keeper.readiness import CheckStatus
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


class BlockedSink(Sink):
    """Holds every write until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def __call__(self, entries: Sequence[NewAuditEntry]) -> None:
        await self.release.wait()
        await super().__call__(entries)


def dropped_points(reader: InMemoryMetricReader) -> dict[tuple[Any, Any], Any]:
    data = reader.get_metrics_data()
    if data is None:
        return {}
    return {
        (point.attributes["reason"], point.attributes["sensitive"]): point.value
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "lorebase.audit.reads.dropped"
        for point in metric.data.data_points
    }


STREAM = "/api/v1/domains/{domain_id}/kinds/{kind}/stream"


def with_stream(app: FastAPI, seen: list[int]) -> FastAPI:
    """A streamed read route; `seen` gets the auditor's pending records as the body starts."""
    router = APIRouter()

    @router.get(STREAM)
    async def stream(
        domain_id: str, kind: str, access: Annotated[Access, Depends(require(Permission.DATA_READ))]
    ) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            seen.append(app.state.read_audit.pending)
            for part in (b"one\n", b"two\n"):
                yield part

        return StreamingResponse(body(), media_type="application/x-ndjson")

    app.include_router(router)
    return app


@pytest.mark.parametrize("on_overflow", ["drop", "reject"])
async def test_reject_refuses_reads_whose_record_has_no_place(
    on_overflow: str, auth_app: Callable[..., FastAPI], client_for: ClientFor, headers: Any
) -> None:
    sink = BlockedSink()
    settings = {
        "enabled": True,
        "on_overflow": on_overflow,
        "queue_size": 2,
        "batch_size": 1,
        "retry_after_s": 7,
    }
    app = with_stream(auth_app(auth(settings), read_audit_sink=sink), [])
    admin = headers("svc_admin")
    async with client_for(app) as http:
        responses = [await http.get(KEYS, headers=admin) for _ in range(5)]
        streamed = await http.get(STREAM.format(domain_id="acme", kind="order"), headers=admin)
        sink.release.set()
        served = sum(r.status_code == 200 for r in responses)
        entries = await read_entries(app, sink)
    if on_overflow == "drop":
        assert served == 5
        assert len(entries) < served  # served reads went unrecorded
        assert streamed.text == "one\ntwo\n"
        return
    refused = [r for r in responses if r.status_code == 503]
    assert refused
    assert served
    assert len(entries) == served  # every served read has its record
    assert refused[0].headers["retry-after"] == "7"
    assert refused[0].headers["content-type"] == "application/problem+json"
    assert refused[0].json()["code"] == "read-audit-unavailable"
    # the 503 replaced the streamed body before any of it left
    assert streamed.status_code == 503
    assert streamed.json()["code"] == "read-audit-unavailable"


async def test_reject_holds_the_place_of_the_record_before_the_body(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, sink: Sink, headers: Any
) -> None:
    seen: list[int] = []
    settings = {"enabled": True, "on_overflow": "reject"}
    app = with_stream(auth_app(auth(settings), read_audit_sink=sink), seen)
    assert any(isinstance(check, ReadAuditCheck) for check in app.state.readiness_checks)
    admin = headers("svc_admin")
    async with client_for(app) as http:
        response = await http.get(STREAM.format(domain_id="acme", kind="order"), headers=admin)
        assert response.text == "one\ntwo\n"
    assert seen == [1]  # held while the body was produced
    [entry] = await read_entries(app, sink)
    assert entry.details["kind"] == "order"
    assert app.state.read_audit.pending == 0


async def test_reject_keeps_failing_batches_and_turns_readiness_failed() -> None:
    class CountingSink(Sink):
        def __init__(self) -> None:
            super().__init__()
            self.retried = asyncio.Event()

        async def __call__(self, entries: Sequence[NewAuditEntry]) -> None:
            if self.attempts >= 3:
                self.retried.set()
            await super().__call__(entries)

    reader = InMemoryMetricReader()
    sink = CountingSink()
    settings = ReadAuditSettings(
        enabled=True, on_overflow="reject", flush_interval_s=0.01, unready_after_failures=2
    )
    reads = ReadAuditor(settings, sink, meter_provider=MeterProvider(metric_readers=[reader]))
    check = ReadAuditCheck(reads)
    assert (await check()).status is CheckStatus.OK
    sink.fail = True
    slot = reads.reserve(sensitive=False)
    assert slot is not None
    slot.fill(NewAuditEntry(actor="usr_1", action="read"))
    async with asyncio.timeout(5):
        await sink.retried.wait()
    assert (await check()).status is CheckStatus.FAILED
    sink.fail = False
    async with asyncio.timeout(5):
        await reads.flush()
    assert (await check()).status is CheckStatus.OK
    assert [e.actor for e in sink.entries] == ["usr_1"]
    await reads.aclose()
    assert dropped_points(reader) == {}


async def test_held_places_count_and_are_given_back() -> None:
    settings = ReadAuditSettings(enabled=True, on_overflow="reject", queue_size=3)
    reads = ReadAuditor(settings, BlockedSink())
    plain = NewAuditEntry(actor="usr_1", action="read")
    first, second = reads.reserve(False), reads.reserve(False)
    assert first is not None
    assert second is not None
    assert reads.reserve(False) is None  # the last place is kept for sensitive reads
    assert not reads.offer(plain)
    assert reads.reserve(True) is not None
    assert reads.reserve(True) is None  # full
    first.release()
    first.fill(plain)  # no effect once released
    assert reads.pending == 2
    assert reads.reserve(True) is not None
    await reads.aclose()
