"""One audit entry per mutating or denied request, and never in the way of the response."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lorebase_keeper.auth.audit import AuditWriter, outcome_of
from lorebase_keeper.auth.config import AuditSettings
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_platform.telemetry import Telemetry
from lorebase_store.auth import NewAuditEntry
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
API_KEYS: dict[str, Any] = {"mode": "api_key", "api_keys": {"pepper": PEPPER}}
KEYS = "/api/v1/domains/acme/api-keys"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def admin_headers(store: Any, codec: Any) -> dict[str, str]:
    store.add_principal("svc_admin", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_admin", "admin")
    token, _ = store.add_key(codec, "svc_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_a_mutation_writes_one_entry_with_who_what_where(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.post(KEYS, json={"name": "ingest bot"}, headers=admin_headers)
    assert response.status_code == 201
    [entry] = store.audit_rows
    assert entry.actor == "svc_admin"
    assert entry.action == "apikey.create"
    assert entry.domain_id == "acme"
    assert entry.target == response.json()["id"]
    assert entry.details["outcome"] == "success"
    assert entry.details["status"] == 201
    assert entry.details["method"] == "POST"
    assert entry.details["route"] == "/api/v1/domains/{domain_id}/api-keys"
    assert entry.details["permission"] == "apikey.manage"
    assert entry.details["request_id"] == response.headers["x-request-id"]


async def test_secrets_never_reach_the_audit_log(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        created = (await http.post(KEYS, json={"name": "bot"}, headers=admin_headers)).json()
    dump = json.dumps([e.details for e in store.audit_rows])
    assert created["key"] not in dump
    assert created["key"].split("_", 2)[2] not in dump
    assert PEPPER not in dump


async def test_reads_are_not_audited(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        assert (await http.get(KEYS, headers=admin_headers)).status_code == 200
        assert (await http.get("/healthz")).status_code == 200
    assert store.audit_rows == []


async def test_a_denied_request_is_audited_with_the_missing_permission(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_viewer", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_viewer", "viewer")
    token, _ = store.add_key(codec, "svc_viewer")
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.get(KEYS, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    [entry] = store.audit_rows
    assert entry.actor == "svc_viewer"
    assert entry.domain_id == "acme"
    assert entry.details["outcome"] == "denied"
    assert entry.details["permission"] == "apikey.manage"
    assert entry.action == "GET /api/v1/domains/{domain_id}/api-keys"


async def test_an_unauthenticated_request_is_audited_as_anonymous(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.post(KEYS, json={"name": "x"})
    assert response.status_code == 401
    [entry] = store.audit_rows
    assert entry.actor == "anonymous"
    assert entry.details["outcome"] == "denied"
    assert entry.details["status"] == 401


async def test_denied_requests_can_be_left_out(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    app = auth_app({**API_KEYS, "audit": {"record_denied": False}})
    async with client_for(app) as http:
        assert (await http.post(KEYS, json={"name": "x"})).status_code == 401
        assert (await http.post(KEYS, json={"name": "x"}, headers=admin_headers)).status_code == 201
    assert [e.action for e in store.audit_rows] == ["apikey.create"]


async def test_rejected_and_unmatched_requests(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        invalid = await http.post(KEYS, json={"name": ""}, headers=admin_headers)
        missing = await http.post("/api/v1/nowhere", json={}, headers=admin_headers)
    assert invalid.status_code == 422
    assert missing.status_code == 404
    [entry] = store.audit_rows  # nothing was authenticated for the unknown route
    assert entry.details["outcome"] == "rejected"
    assert entry.details["status"] == 422


async def test_a_server_error_is_audited_as_error(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    app = auth_app(API_KEYS)
    router = APIRouter()

    @router.post("/api/v1/domains/{domain_id}/explode")
    async def explode(
        access: Annotated[Access, Depends(require(Permission.DOMAIN_SETTINGS))],
    ) -> None:
        raise RuntimeError("boom")

    app.include_router(router)
    async with client_for(app) as http:
        response = await http.post("/api/v1/domains/acme/explode", headers=admin_headers)
    assert response.status_code == 500
    [entry] = store.audit_rows
    assert entry.details["outcome"] == "error"
    assert entry.details["status"] == 500


async def test_mode_none_audits_mutations_when_a_database_is_present(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any
) -> None:
    async with client_for(auth_app({"mode": "none"})) as http:
        response = await http.post("/api/v1/domains/acme/grants", json={})
    assert response.status_code == 422
    async with client_for(auth_app({"mode": "none"})) as http:
        created = await http.post(
            "/api/v1/domains/acme/grants",
            json={"subject": "usr_x", "role": "viewer"},
        )
    assert created.status_code == 404  # unknown subject
    assert [e.actor for e in store.audit_rows] == ["anonymous", "anonymous"]


async def test_audit_can_be_switched_off(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    app = auth_app({**API_KEYS, "audit": {"enabled": False}})
    async with client_for(app) as http:
        assert (await http.post(KEYS, json={"name": "x"}, headers=admin_headers)).status_code == 201
    assert store.audit_rows == []


async def test_a_failing_audit_write_does_not_change_the_response(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    admin_headers: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.audit_failure = RuntimeError("audit table unavailable")
    caplog.set_level(logging.ERROR, logger="lorebase.keeper.audit")
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.post(KEYS, json={"name": "x"}, headers=admin_headers)
    assert response.status_code == 201
    assert response.json()["key"].startswith("lbk_")
    assert store.audit_rows == []
    assert any("audit entry dropped" in record.message for record in caplog.records)


async def test_a_slow_audit_write_is_abandoned_at_the_timeout(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    store.audit_delay = 5
    app = auth_app({**API_KEYS, "audit": {"write_timeout_s": 0.05}})
    async with client_for(app) as http:
        response = await http.post(KEYS, json={"name": "x"}, headers=admin_headers)
    assert response.status_code == 201
    assert store.audit_rows == []


async def test_failed_writes_are_counted_in_a_metric(store: Any) -> None:
    reader = InMemoryMetricReader()
    writer = AuditWriter(store, AuditSettings(), MeterProvider(metric_readers=[reader]))
    store.audit_failure = RuntimeError("down")
    assert not await writer.write(NewAuditEntry(actor="a", action="x"))
    store.audit_failure = None
    assert await writer.write(NewAuditEntry(actor="a", action="y"))

    data = reader.get_metrics_data()
    assert data is not None
    points = [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "lorebase.audit.write_failures"
        for point in metric.data.data_points
    ]
    assert [p.value for p in points] == [1]
    assert points[0].attributes == {"reason": "RuntimeError"}


async def test_the_metric_reaches_the_application_telemetry(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, admin_headers: Any
) -> None:
    reader = InMemoryMetricReader()
    telemetry = Telemetry(enabled=True, meter_provider=MeterProvider(metric_readers=[reader]))
    store.audit_failure = RuntimeError("down")
    app = auth_app(API_KEYS, telemetry=telemetry)
    async with client_for(app) as http:
        await http.post(KEYS, json={"name": "x"}, headers=admin_headers)
    names = {
        metric.name
        for resource in reader.get_metrics_data().resource_metrics  # type: ignore[union-attr]
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert "lorebase.audit.write_failures" in names


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, "success"),
        (201, "success"),
        (302, "success"),
        (400, "rejected"),
        (401, "denied"),
        (403, "denied"),
        (404, "rejected"),
        (422, "rejected"),
        (500, "error"),
        (503, "error"),
    ],
)
def test_outcome_of(status: int, outcome: str) -> None:
    assert outcome_of(status) == outcome
