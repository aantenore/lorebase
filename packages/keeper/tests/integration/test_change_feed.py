"""Change feed and webhooks against the `supply` example in Postgres.

The tests of this module commit to the shared seeded Domain; each one reads the head it starts
from instead of assuming the commits of the others.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_keeper.services.webhooks import HttpxSender
from lorebase_store.db import create_engine
from lorebase_store.materialize import create_branch
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings
from lorebase_store.webhooks import WebhookSettings, deliver_pending, verify

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
FAST_FEED = {"feed": {"poll_interval_s": 0.05, "heartbeat_s": 0.3, "max_connection_s": 1.5}}
LOCAL_WEBHOOKS = {"allowed_schemes": ["http", "https"], "allow_private_targets": True}


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, (_, _, data) = await load_supply(session, runtime, clock)
    return Seeded(
        owner_url=owner_url,
        app_url=app_url,
        schema=schema,
        revisions={"data": data.revision_id.hex()},
    )


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


class Supplier:
    """Commits risk score changes of a supplier through the store, as the owner role."""

    def __init__(self, seeded: Seeded, code: str) -> None:
        self._seeded = seeded
        self._row = next(r for r in rows("supplier.csv") if r["supplier_code"] == code)
        self._clock = Clock()

    async def set_risk(self, score: int, branch: str = "main") -> str:
        schema: Schema = self._seeded.schema
        kind = schema.kind_by_api_name("Supplier")
        assert kind is not None
        async with owner_session(self._seeded.owner_url) as session:
            result = await commit_changes(
                session,
                StoreRuntime.from_settings(StoreSettings()),
                self._clock,
                [record_change(kind, {**self._row, "risk_score": str(score)})],
                message=f"Set the risk of {self._row['supplier_code']} to {score}",
                branch=branch,
            )
        return result.revision_id.hex()


def parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            name, _, value = line.partition(": ")
            fields[name] = value
        if fields.get("event") == "revision":
            events.append({"id": int(fields["id"]), **json.loads(fields["data"])})
    return events


async def head_of(http: httpx.AsyncClient, branch: str = "main") -> str:
    response = await http.get(f"{BASE}/branches/{branch}/changes", params={"limit": 1})
    assert response.status_code == 200, response.text
    return str(response.json()["head"])


# Server-Sent Events -----------------------------------------------------------------------


async def test_stream_receives_a_commit_and_resumes(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, agents=FAST_FEED) as http:
        listening = asyncio.create_task(
            http.get(f"{BASE}/branches/main/events", params={"kinds": "Supplier"})
        )
        await asyncio.sleep(0.4)  # the stream has taken its starting point
        revision = await Supplier(seeded, "S-1002").set_risk(77)
        response = await listening
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.text
        assert body.startswith("retry: 3000\n\n")
        events = parse_sse(body)
        assert [e["revision"] for e in events] == [revision]
        (event,) = events
        assert event["type"] == "revision.committed"
        assert event["branch"] == "main"
        assert event["kinds"] == {"Supplier": 1}
        assert event["keys"] == {"Supplier": ["S-1002"]}
        assert event["origin"] == {"type": "operation", "ref": None}
        assert event["author"] == "usr_editor"
        assert event["sequence"] == event["id"]
        assert ": keep-alive" in body

        # Resume after the event before: the commit is sent again.
        resumed = await http.get(
            f"{BASE}/branches/main/events", headers={"Last-Event-ID": str(event["id"] - 1)}
        )
        assert revision in [e["revision"] for e in parse_sse(resumed.text)]
        # A Kind filter the commit does not touch leaves it out.
        other = await http.get(
            f"{BASE}/branches/main/events",
            params={"kinds": "Plant", "last_event_id": str(event["id"] - 1)},
        )
        assert parse_sse(other.text) == []
        bad = await http.get(f"{BASE}/branches/main/events", headers={"Last-Event-ID": "x"})
        assert bad.status_code == 422
        missing = await http.get(f"{BASE}/branches/nope/events")
        assert missing.status_code == 404


# Polling fallback --------------------------------------------------------------------------


async def test_changes_since_a_revision(seeded: Seeded) -> None:
    supplier = Supplier(seeded, "S-1003")
    async with keeper(seeded.app_url) as http:
        before = await head_of(http)
        first = await supplier.set_risk(40)
        second = await supplier.set_risk(41)

        page = (await http.get(f"{BASE}/branches/main/changes", params={"since": before})).json()
        assert page["since"] == before
        assert [item["revision"] for item in page["items"]] == [first, second]
        assert page["items"][0]["keys"] == {"Supplier": ["S-1003"]}
        assert page["next_since"] == second
        assert page["more"] is False

        paged = (
            await http.get(f"{BASE}/branches/main/changes", params={"since": before, "limit": 1})
        ).json()
        assert [item["revision"] for item in paged["items"]] == [first]
        assert paged["next_since"] == first
        assert paged["more"] is True

        caught_up = (
            await http.get(f"{BASE}/branches/main/changes", params={"since": second})
        ).json()
        assert caught_up["items"] == []
        assert caught_up["next_since"] == second

        filtered = (
            await http.get(
                f"{BASE}/branches/main/changes", params={"since": before, "kinds": "Plant"}
            )
        ).json()
        assert filtered["items"] == []
        assert filtered["next_since"] == second

        latest = (await http.get(f"{BASE}/branches/main/changes", params={"limit": 2})).json()
        assert [item["revision"] for item in latest["items"]] == [first, second]

        async with owner_session(seeded.owner_url) as session:
            await create_branch(
                session,
                DOMAIN,
                f"feed/{second[:8]}",
                created_by="usr_editor",
                runtime=StoreRuntime.from_settings(StoreSettings()),
                clock=Clock(),
            )
        elsewhere = await supplier.set_risk(42, branch=f"feed/{second[:8]}")
        diverged = await http.get(f"{BASE}/branches/main/changes", params={"since": elsewhere})
        assert diverged.status_code == 409
        unknown = await http.get(f"{BASE}/branches/main/changes", params={"kinds": "Nope"})
        assert unknown.status_code == 422


# Webhooks -----------------------------------------------------------------------------------


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    """A local HTTP endpoint recording the requests it gets; `/fail` answers 500."""

    requests: list[Received] = field(default_factory=list)
    url: str = ""


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    state = Receiver()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.requests.append(
                Received(self.path, dict(self.headers.items()), self.rfile.read(length))
            )
            self.send_response(500 if self.path == "/fail" else 204)
            self.end_headers()

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@asynccontextmanager
async def app_sessions(url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(DatabaseSettings(), url=url, pooled=False)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_webhook_delivery_with_signature_and_retries(
    seeded: Seeded, receiver: Receiver
) -> None:
    settings = WebhookSettings(**LOCAL_WEBHOOKS)  # type: ignore[arg-type]
    writes = StoreSettings().writes
    async with (
        keeper(seeded.app_url, webhooks=LOCAL_WEBHOOKS) as http,
        app_sessions(seeded.app_url) as sessions,
        httpx.AsyncClient(trust_env=False) as client,
    ):
        send = HttpxSender(client)
        await deliver_pending(sessions, settings, send, writes=writes)  # drain older events

        refused = await http.post(f"{BASE}/webhooks", json={"url": "ftp://example.com/x"})
        assert refused.status_code == 422
        created = await http.post(
            f"{BASE}/webhooks",
            json={"url": f"{receiver.url}/hook", "branches": ["main"], "description": "tests"},
        )
        assert created.status_code == 201, created.text
        hook = created.json()
        secret = hook["secret"]
        assert hook["events"] == ["revision.committed"]
        failing = (await http.post(f"{BASE}/webhooks", json={"url": f"{receiver.url}/fail"})).json()
        listed = (await http.get(f"{BASE}/webhooks")).json()["items"]
        assert [item["id"] for item in listed] == [hook["id"], failing["id"]]
        assert all("secret" not in item for item in listed)

        revision = await Supplier(seeded, "S-1004").set_risk(66)
        report = await deliver_pending(sessions, settings, send, writes=writes)
        assert (report.queued, report.delivered, report.retrying) == (2, 1, 1)

        delivered = [r for r in receiver.requests if r.path == "/hook"]
        assert len(delivered) == 1
        request = delivered[0]
        payload = json.loads(request.body)
        assert payload["revision"] == revision
        assert payload["keys"] == {"Supplier": ["S-1004"]}
        assert request.headers["Lorebase-Event"] == "revision.committed"
        assert verify(
            secret,
            request.headers["Lorebase-Signature"],
            request.body,
            now=int(time.time()),
            tolerance_s=300,
        )
        assert not verify(
            failing["secret"],
            request.headers["Lorebase-Signature"],
            request.body,
            now=int(time.time()),
            tolerance_s=300,
        )

        again = await deliver_pending(sessions, settings, send, writes=writes)
        assert again.attempted == 0  # the failed one waits for its backoff

        async with owner_session(seeded.owner_url) as session:
            state = (
                await session.execute(
                    text(
                        "SELECT status, attempts, last_status, next_attempt_at > now() "
                        "FROM webhook_deliveries WHERE webhook_id = :w"
                    ),
                    {"w": failing["id"]},
                )
            ).one()
        assert tuple(state) == ("pending", 1, 500, True)

        assert (await http.delete(f"{BASE}/webhooks/{failing['id']}")).status_code == 204
        assert (await http.delete(f"{BASE}/webhooks/{failing['id']}")).status_code == 404
        async with owner_session(seeded.owner_url) as session:
            actions = list(
                (
                    await session.execute(
                        text(
                            "SELECT action, target FROM audit_log WHERE domain_id = :d "
                            "AND action LIKE 'webhook.%' ORDER BY seq"
                        ),
                        {"d": DOMAIN},
                    )
                ).all()
            )
            remaining = await session.scalar(
                text("SELECT count(*) FROM webhook_deliveries WHERE webhook_id = :w"),
                {"w": failing["id"]},
            )
        assert [tuple(a) for a in actions] == [
            ("webhook.create", hook["id"]),
            ("webhook.create", failing["id"]),
            ("webhook.delete", failing["id"]),
        ]
        assert remaining == 0
