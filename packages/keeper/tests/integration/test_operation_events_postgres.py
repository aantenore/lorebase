"""Side effects of stored Operations (design 5.7, 10.5) on the `supply` example: events are
written with the commit or not at all, never by dry runs, refusals or replays, and webhooks
receive sensitive values only when cleared for them."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.services import operations
from lorebase_keeper.services.operation_events import write_side_effects
from lorebase_store.db import create_engine
from lorebase_store.materialize import create_branch
from lorebase_store.repositories.deferred_events import DeferredEventRepository, expire
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings
from lorebase_store.webhooks import WebhookSettings, deliver_pending

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import DOMAIN, Clock, load_supply

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
BRANCH = "events"
FLAG = "operation.risk_flag_raised"
SET_VAT = {
    "description": "Record a supplier's VAT number.",
    "parameters": [
        {"name": "supplier", "type": "ref(Supplier)", "required": True},
        {"name": "vat", "type": "string", "required": True},
    ],
    "steps": [
        {
            "step": "update_record",
            "kind": "Supplier",
            "target": "params.supplier",
            "values": {"vat_number": "params.vat"},
        }
    ],
    "side_effects": ["vat_changed"],
}


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    await create_branch(
        session, DOMAIN, BRANCH, created_by="usr_editor", runtime=runtime, clock=clock
    )
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def run(http: Any, operation: str, parameters: dict[str, Any], **extra: Any) -> Any:
    return await http.post(
        f"{BASE}/operations/{operation}/execute",
        json={"branch": BRANCH, "parameters": parameters, **extra},
    )


async def events(seeded: Seeded, event_type: str) -> list[tuple[int, dict[str, Any]]]:
    async with owner_session(seeded.owner_url) as session:
        result = await session.execute(
            text("SELECT id, payload FROM outbox WHERE event_type = :t ORDER BY id"),
            {"t": event_type},
        )
        return [(row.id, row.payload) for row in result]


async def head(seeded: Seeded) -> bytes:
    async with owner_session(seeded.owner_url) as session:
        found = await session.scalar(
            text("SELECT head FROM refs WHERE domain_id = :d AND name = :b"),
            {"d": DOMAIN, "b": BRANCH},
        )
    assert found is not None
    return bytes(found)


async def test_a_committed_execution_writes_its_side_effect(seeded: Seeded) -> None:
    before = len(await events(seeded, FLAG))
    async with keeper(seeded.app_url) as http:
        flagged = await run(
            http,
            "flag_supplier_risk",
            {"supplier": "S-1001", "severity": "high", "reason": "Two late deliveries"},
        )
        assert flagged.status_code == 200, flagged.text
    body = flagged.json()
    written = await events(seeded, FLAG)
    assert len(written) == before + 1
    _, payload = written[-1]
    assert payload["side_effect"] == "risk_flag_raised"
    assert payload["operation"] == "flag_supplier_risk"
    assert payload["invocation_id"] == body["invocation_id"]
    assert payload["branch"] == BRANCH
    assert payload["revisions"] == body["revisions"]
    assert payload["parameters"]["severity"] == "high"
    assert [edit.get("kind", edit.get("relation")) for edit in payload["edits"]] == [
        "RiskFlag",
        "flags",
    ]
    assert payload["edits_truncated"] is False


async def test_dry_runs_refusals_and_replays_write_nothing(seeded: Seeded) -> None:
    before = len(await events(seeded, FLAG))
    params = {"supplier": "S-1001", "severity": "low", "reason": "Late paperwork"}
    async with keeper(seeded.app_url) as http:
        dry = await run(http, "flag_supplier_risk", params, dry_run=True)
        assert dry.status_code == 200
        assert dry.json()["status"] == "dry_run"
        critical = await run(
            http, "flag_supplier_risk", {**params, "severity": "critical"}
        )  # review required
        assert critical.status_code == 409
        proposed = await run(
            http,
            "flag_supplier_risk",
            {**params, "severity": "critical"},
            task="events",
            auto_propose=True,
        )  # committed on a task branch whose Proposal reviews it: no event
        assert proposed.status_code == 202, proposed.text
        terminated = await run(http, "flag_supplier_risk", {**params, "supplier": "S-1019"})
        assert terminated.status_code == 422
        assert len(await events(seeded, FLAG)) == before

        first = await run(http, "flag_supplier_risk", params, idempotency_key="flag-once")
        assert first.status_code == 200, first.text
        again = await run(http, "flag_supplier_risk", params, idempotency_key="flag-once")
        assert again.status_code == 200
        assert again.json()["invocation_id"] == first.json()["invocation_id"]
    assert len(await events(seeded, FLAG)) == before + 1


async def deferred(seeded: Seeded) -> int:
    async with owner_session(seeded.owner_url) as session:
        found = await session.scalar(text("SELECT count(*) FROM operation_deferred_events"))
    return int(found or 0)


async def test_a_reviewed_execution_writes_its_events_once_when_its_proposal_merges(
    seeded: Seeded,
) -> None:
    params = {"supplier": "S-1005", "severity": "critical", "reason": "Audit finding"}
    before = len(await events(seeded, FLAG))
    async with keeper(seeded.app_url) as http:
        proposed = await run(
            http, "flag_supplier_risk", params, task="deferred-flag", auto_propose=True
        )
        assert proposed.status_code == 202, proposed.text
        body = proposed.json()
        assert len(await events(seeded, FLAG)) == before
        assert await deferred(seeded) >= 1

        proposal = body["proposal"]["id"]
        merged = await http.post(f"{BASE}/proposals/{proposal}/merge")
        assert merged.status_code == 200, merged.text
        written = await events(seeded, FLAG)
        assert len(written) == before + 1
        _, payload = written[-1]
        assert payload["invocation_id"] == body["invocation_id"]
        assert payload["branch"] == BRANCH
        assert payload["source_branch"] == body["branch"]
        assert payload["proposal"] == proposal
        assert payload["head"] == merged.json()["revision"]
        assert payload["revisions"] == body["revisions"]
        assert payload["parameters"]["severity"] == "critical"

        again = await http.post(f"{BASE}/proposals/{proposal}/merge")
        assert again.status_code != 200 or again.json()["revision"] == payload["head"]
    assert len(await events(seeded, FLAG)) == before + 1
    async with owner_session(seeded.owner_url) as session:
        left = await session.scalar(
            text("SELECT count(*) FROM operation_deferred_events WHERE branch = :b"),
            {"b": body["branch"]},
        )
    assert left == 0


async def test_deferred_events_wait_for_a_merge_that_contains_their_revision(
    seeded: Seeded,
) -> None:
    async with owner_session(seeded.owner_url) as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        repository = DeferredEventRepository(session, DOMAIN)
        await repository.defer(
            branch="elsewhere",
            revision=b"\x01" * 32,  # not in the merged history
            invocation_id="inv_x",
            event_type=FLAG,
            payload={"side_effect": "risk_flag_raised"},
        )
        assert await repository.release("elsewhere", await head(seeded), {"branch": BRANCH}) == []
        assert await repository.discard("elsewhere") == 1

        await repository.defer(
            branch="forgotten",
            revision=b"\x02" * 32,
            invocation_id="inv_y",
            event_type=FLAG,
            payload={"side_effect": "risk_flag_raised"},
        )
        assert await expire(session, datetime.now(UTC) - timedelta(days=1), limit=10) == 0
        assert await expire(session, datetime.now(UTC) + timedelta(seconds=1), limit=10) >= 1


async def test_a_failure_after_the_events_rolls_everything_back(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = write_side_effects

    async def write_then_fail(*args: Any, **kwargs: Any) -> list[int]:
        assert await written(*args, **kwargs)
        raise RuntimeError("the transaction fails after the events were written")

    monkeypatch.setattr(operations, "write_side_effects", write_then_fail)
    before, start = len(await events(seeded, FLAG)), await head(seeded)
    async with owner_session(seeded.owner_url) as session:
        invocations = await session.scalar(text("SELECT count(*) FROM operation_invocations"))
    async with keeper(seeded.app_url) as http:
        failed = await run(
            http,
            "flag_supplier_risk",
            {"supplier": "S-1001", "severity": "medium", "reason": "Never committed"},
        )
        assert failed.status_code == 500
    assert len(await events(seeded, FLAG)) == before
    assert await head(seeded) == start
    async with owner_session(seeded.owner_url) as session:
        assert await session.scalar(text("SELECT count(*) FROM operation_invocations")) == (
            invocations
        )


class Sender:
    def __init__(self) -> None:
        self.bodies: dict[str, dict[str, Any]] = {}

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        self.bodies[urlsplit(url).hostname or ""] = json.loads(body)
        return 204


async def public(host: str) -> Sequence[str]:
    return ["93.184.215.14"]


@asynccontextmanager
async def app_sessions(url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(DatabaseSettings(), url=url, pooled=False)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_webhooks_receive_sensitive_values_only_with_clearance(seeded: Seeded) -> None:
    settings = WebhookSettings()
    writes = StoreSettings().writes
    async with keeper(seeded.app_url) as http, app_sessions(seeded.app_url) as sessions:
        await deliver_pending(sessions, settings, Sender(), writes=writes, resolve=public)
        unknown = await http.post(
            f"{BASE}/webhooks", json={"url": "https://x.example.com/", "events": ["ops.x"]}
        )
        assert unknown.status_code == 422
        plain = await http.post(
            f"{BASE}/webhooks",
            json={"url": "https://plain.example.com/hook", "events": ["operation.vat_changed"]},
        )
        assert plain.status_code == 201, plain.text
        assert plain.json()["clearances"] == []
        cleared = await http.post(
            f"{BASE}/webhooks",
            json={"url": "https://cleared.example.com/hook", "clearances": ["confidential"]},
        )
        assert cleared.status_code == 201, cleared.text
        assert cleared.json()["events"] == ["operation.*", "revision.committed"]
        assert cleared.json()["clearances"] == ["confidential"]

        added = await http.post(
            f"{BASE}/branches/{BRANCH}/schema/changes",
            json={"patch": {"operations": {"set_vat": SET_VAT}}},
        )
        assert added.status_code == 200, added.text
        done = await run(http, "set_vat", {"supplier": "S-1002", "vat": "IT-EVENTS-0001"})
        assert done.status_code == 200, done.text

        [(_, stored)] = await events(seeded, "operation.vat_changed")
        [edit] = stored["edits"]
        assert edit["values"]["vat_number"] == {
            "$sensitive": {"tags": ["confidential"], "value": "IT-EVENTS-0001"}
        }
        assert stored["parameters"] == {"supplier": "***", "vat": "***"}

        send = Sender()
        await deliver_pending(sessions, settings, send, writes=writes, resolve=public)
    assert send.bodies["plain.example.com"]["edits"][0]["values"]["vat_number"] == "***"
    assert send.bodies["cleared.example.com"]["edits"][0]["values"]["vat_number"] == (
        "IT-EVENTS-0001"
    )
    assert send.bodies["cleared.example.com"]["type"] == "operation.vat_changed"
