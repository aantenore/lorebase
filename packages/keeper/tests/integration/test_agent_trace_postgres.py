"""Agent trace against the `supply` example in Postgres: steps of a traced session, notes,
query plans, visibility, the per-session cap and pruning."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from lorebase_store.tracing import (
    NewTraceStep,
    TraceRepository,
    TraceSessionSummary,
    TraceVisibility,
)

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import DOMAIN, rows
from .test_agents_api import seed

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
TRACE = "/api/v1/trace/sessions"
SESSION = "sess-trace-1"
HEADERS = {
    "X-Lorebase-Session": SESSION,
    "X-Lorebase-Tool": "lorebase_search",
    "X-Lorebase-Trace": "debug",
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
}


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def trace_section(**extra: Any) -> dict[str, Any]:
    return {"trace": {"enabled": True, "flush_interval_s": 0.01, **extra}}


async def timeline(http: httpx.AsyncClient, session: str, steps: int) -> dict[str, Any]:
    """The timeline once it holds `steps` steps (writes happen off the request path)."""
    for _ in range(100):
        response = await http.get(f"{TRACE}/{session}")
        if response.status_code == 200 and len(response.json()["steps"]) >= steps:
            body: dict[str, Any] = response.json()
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeline of {session} never reached {steps} steps")


async def test_a_traced_session_records_queries_counts_refs_and_notes(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, trace_section()) as http:
        query = {
            "kind": "Supplier",
            "where": {"attr": "country", "op": "eq", "value": "PL"},
            "limit": 5,
        }
        found = await http.post(f"{BASE}/query", json=query, headers=HEADERS)
        assert found.status_code == 200, found.text
        typo = await http.post(
            f"{BASE}/search", json={"q": "Kavsen Aloys", "kinds": ["Supplier"]}, headers=HEADERS
        )
        assert typo.status_code == 200
        note = await http.post(
            f"{TRACE}/{SESSION}/notes",
            json={"text": "<b>check</b> the supplier", "step": 2, "domain_id": DOMAIN},
            headers=HEADERS,
        )
        assert note.status_code == 202
        assert note.json() == {"recorded": True}
        for _ in range(2):  # the second answer comes from the response cache
            record = await http.get(f"{BASE}/kinds/Supplier/records/S-1001", headers=HEADERS)
            assert record.status_code == 200
        untraced = await http.get(f"{BASE}/kinds/Supplier/records/S-1001")
        assert untraced.status_code == 200
        hood = await http.post(
            f"{BASE}/neighborhood",
            json={"seeds": [{"kind": "Supplier", "key": "S-1005"}], "depth": 1},
            headers=HEADERS,
        )
        assert hood.status_code == 200

        body = await timeline(http, SESSION, 6)
        steps = body["steps"]
        assert [s["step"] for s in steps] == list(range(1, 7))
        assert [s["type"] for s in steps] == ["call", "call", "note", "call", "call", "call"]
        first, search, noted, cold, warm, graph = steps

        assert first["route"] == "/api/v1/domains/{domain_id}/query"
        assert first["tool"] == "lorebase_search"
        assert first["domain_id"] == DOMAIN
        assert first["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert first["query"]["body"]["where"]["value"] == {"redacted": "string"}
        assert first["result"]["counts"]["items"] == len(found.json()["items"])
        assert first["explain"]["plans"][0]["label"] == "query"
        assert first["explain"]["plans"][0]["node_types"]

        assert search["query"]["body"]["q"] == "Kavsen Aloys"
        assert search["result"]["corrections"][0]["word"] == "aloys"
        assert {"kind": "Supplier", "key": "S-1001"} in search["refs"]["records"]

        assert noted["note"] == "<b>check</b> the supplier"
        assert noted["result"] == {"agent_step": 2}

        assert (cold["cache"], warm["cache"]) == ("miss", "hit")
        assert cold["refs"]["records"] == [{"kind": "Supplier", "key": "S-1001"}]

        assert graph["refs"]["links"]
        hub = {"kind": "Supplier", "key": "S-1005"}
        assert any(hub in (link["from"], link["to"]) for link in graph["refs"]["links"])

        dumped = json.dumps(body)
        supplier = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1001")
        assert supplier["legal_name"] not in dumped  # never values

        listed = (await http.get(TRACE)).json()
        assert listed["enabled"] is True
        [summary] = [s for s in listed["items"] if s["session_id"] == SESSION]
        assert (summary["calls"], summary["notes"]) == (5, 1)
        assert summary["tools"] == ["lorebase_search"]
        assert summary["domains"] == [DOMAIN]


async def test_literals_are_kept_when_configured_and_untraced_requests_are_not(
    seeded: Seeded,
) -> None:
    section = trace_section(
        capture_literals=True, allow_header_opt_in=False, principals=["anonymous"]
    )
    async with keeper(seeded.app_url, section) as http:
        headers = {"X-Lorebase-Session": "sess-literals"}
        query = {"kind": "Supplier", "where": {"attr": "country", "op": "eq", "value": "PL"}}
        assert (await http.post(f"{BASE}/query", json=query, headers=headers)).status_code == 200
        assert (await http.post(f"{BASE}/query", json=query)).status_code == 200  # inferred
        bad = {"X-Lorebase-Session": "bad id with spaces"}
        assert (await http.post(f"{BASE}/query", json=query, headers=bad)).status_code == 200
        body = await timeline(http, "sess-literals", 1)
        [step] = body["steps"]
        assert step["query"]["body"]["where"]["value"] == "PL"
        assert step["explain"] is None  # no debug opt-in
        missing = await http.get(f"{TRACE}/never-seen")
        assert missing.status_code == 404


async def test_disabled_tracing_records_nothing(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        note = await http.post(f"{TRACE}/sess-off/notes", json={"text": "x"})
        assert note.json() == {"recorded": False}
        listed = await http.get(TRACE)
        assert listed.json()["enabled"] is False
        too_long = await http.post(f"{TRACE}/sess-off/notes", json={"text": "x" * 5000})
        assert too_long.status_code == 422


async def test_visibility_cap_and_pruning(seeded: Seeded) -> None:
    now = datetime.now(UTC)

    def step(actor: str, session: str, domain: str | None, at: datetime) -> NewTraceStep:
        return NewTraceStep(session_id=session, actor=actor, type="call", at=at, domain_id=domain)

    async with owner_session(seeded.owner_url) as session, session.begin():
        repository = TraceRepository(session)
        written = await repository.append_many(
            [
                step("usr_a", "cap", "d1", now),
                step("usr_a", "cap", "d1", now),
                step("usr_a", "cap", "d1", now),
                step("usr_b", "cap", "d2", now),
                step("usr_b", "old", None, now - timedelta(days=30)),
            ],
            max_per_session=2,
        )
        assert written == 4  # the third step of usr_a in `cap` is over the cap

        own = TraceVisibility(owner="usr_a")
        assert [s.actor for s in await repository.steps("cap", own, limit=10)] == ["usr_a"] * 2
        d2 = TraceVisibility(owner="usr_c", domains=frozenset({"d2"}))
        assert [s.actor for s in await repository.steps("cap", d2, limit=10)] == ["usr_b"]
        admin = TraceVisibility(owner="usr_c", domains=None, domainless=True)
        assert len(await repository.steps("old", admin, limit=10)) == 1
        assert (
            await repository.steps("old", TraceVisibility(owner="usr_c", domains=None), limit=10)
            == []
        )

        sessions = await repository.sessions(admin, limit=10)
        assert {(s.session_id, s.actor) for s in sessions} >= {
            ("cap", "usr_a"),
            ("cap", "usr_b"),
            ("old", "usr_b"),
        }
        first_page = await repository.sessions(admin, limit=1)
        last = first_page[0]
        rest = await repository.sessions(
            admin, limit=10, after=(last.last_at, last.session_id, last.actor)
        )
        assert (last.session_id, last.actor) not in {(s.session_id, s.actor) for s in rest}

        assert await repository.prune(now - timedelta(days=7), limit=100) == 1
        assert await repository.steps("old", admin, limit=10) == []


async def test_stateless_calls_land_in_one_inferred_session(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, trace_section()) as http:
        headers = {"X-Lorebase-Trace": "on", "User-Agent": "stateless-agent/1"}
        for key in ("S-1001", "S-1005"):
            record = await http.get(f"{BASE}/kinds/Supplier/records/{key}", headers=headers)
            assert record.status_code == 200
        explicit = {**headers, "X-Lorebase-Session": "sess-explicit"}
        read = await http.get(f"{BASE}/kinds/Supplier/records/S-1001", headers=explicit)
        assert read.status_code == 200
        await timeline(http, "sess-explicit", 1)
        inferred: list[dict[str, Any]] = []
        for _ in range(100):
            listed = (await http.get(TRACE)).json()["items"]
            inferred = [s for s in listed if s["inferred"] and s["calls"] == 2]
            if inferred:
                break
            await asyncio.sleep(0.05)
        [session] = inferred
        assert session["session_id"].startswith("inferred-")
        steps = (await http.get(f"{TRACE}/{session['session_id']}")).json()["steps"]
        assert [s["inferred"] for s in steps] == [True, True]
        explicit_steps = (await http.get(f"{TRACE}/sess-explicit")).json()["steps"]
        assert [s["inferred"] for s in explicit_steps] == [False]


async def test_inferred_sessions_split_on_idle_gaps_and_never_across_principals(
    seeded: Seeded,
) -> None:
    start = datetime.now(UTC)

    def call(actor: str, seconds: float) -> NewTraceStep:
        return NewTraceStep(
            session_id="",
            actor=actor,
            type="call",
            at=start + timedelta(seconds=seconds),
            inferred=True,
            fingerprint="same-client",
        )

    async with owner_session(seeded.owner_url) as session, session.begin():
        repository = TraceRepository(session)
        await repository.append_many(
            [call("usr_x", 0), call("usr_x", 50), call("usr_y", 60), call("usr_x", 200)],
            max_per_session=100,
            inferred_idle_s=100,
        )
        everyone = TraceVisibility(owner="nobody", domains=None, domainless=True)
        found = [
            s
            for s in await repository.sessions(everyone, limit=50)
            if s.actor in ("usr_x", "usr_y")
        ]
        assert sorted((s.actor, s.calls) for s in found) == [
            ("usr_x", 1),
            ("usr_x", 2),
            ("usr_y", 1),
        ]
        assert all(s.inferred for s in found)
        assert len({s.session_id for s in found}) == 3


async def test_the_cap_holds_across_concurrent_writers(seeded: Seeded) -> None:
    now = datetime.now(UTC)
    batch = [
        NewTraceStep(session_id="race", actor="usr_r", type="call", at=now, domain_id="d1")
        for _ in range(5)
    ]

    async def write() -> int:
        async with owner_session(seeded.owner_url) as session, session.begin():
            written = await TraceRepository(session).append_many(batch, max_per_session=7)
            await asyncio.sleep(0.05)  # hold the session row while the other writers wait
            return written

    results = await asyncio.gather(*(write() for _ in range(4)))
    assert sorted(results) == [0, 0, 2, 5]
    async with owner_session(seeded.owner_url) as session, session.begin():
        own = TraceVisibility(owner="usr_r")
        repository = TraceRepository(session)
        assert len(await repository.steps("race", own, limit=50)) == 7
        [summary] = await repository.sessions(own, limit=10)
        assert (summary.session_id, summary.calls) == ("race", 7)


async def test_sessions_are_listed_from_summaries_kept_by_writes_and_pruning(
    seeded: Seeded,
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=30)

    def step(
        domain: str | None, at: datetime, *, kind: str = "call", tool: str | None = None
    ) -> NewTraceStep:
        return NewTraceStep(
            session_id="summary",
            actor="usr_s",
            type=kind,
            at=at,
            domain_id=domain,
            tool=tool,
            proposal_id="prp_1" if tool == "lorebase_write" else None,
        )

    def mine(found: list[TraceSessionSummary]) -> list[TraceSessionSummary]:
        return [s for s in found if s.actor == "usr_s"]

    async with owner_session(seeded.owner_url) as session, session.begin():
        repository = TraceRepository(session)
        await repository.append_many(
            [
                step("d1", old, tool="lorebase_list"),
                step("d1", now, tool="lorebase_write"),
                step("d2", now, tool="lorebase_query"),
                step(None, now, kind="note"),
            ],
            max_per_session=100,
        )
        await repository.append_many([step("d1", now, tool="lorebase_search")], max_per_session=100)

        everyone = TraceVisibility(owner="nobody", domains=None, domainless=True)
        [whole] = mine(await repository.sessions(everyone, limit=50))
        assert (whole.calls, whole.notes, whole.started_at) == (4, 1, old)
        assert whole.domains == ("d1", "d2")
        assert whole.tools == (
            "lorebase_list",
            "lorebase_query",
            "lorebase_search",
            "lorebase_write",
        )
        assert whole.proposals == ("prp_1",)

        d2 = TraceVisibility(owner="nobody", domains=frozenset({"d2"}))
        [partial] = await repository.sessions(d2, limit=50, actor="usr_s")
        assert (partial.calls, partial.notes, partial.tools) == (1, 0, ("lorebase_query",))
        [filtered] = mine(await repository.sessions(everyone, limit=50, domain_id="d1"))
        assert (filtered.calls, filtered.domains) == (3, ("d1",))

        assert await repository.prune(now - timedelta(days=7), limit=100) >= 1
        [pruned] = mine(await repository.sessions(everyone, limit=50))
        assert (pruned.calls, pruned.notes, pruned.started_at) == (3, 1, now)
        assert pruned.tools == ("lorebase_query", "lorebase_search", "lorebase_write")
        held = await session.execute(
            text(
                "SELECT steps FROM agent_trace_sessions "
                "WHERE actor = 'usr_s' AND session_id = 'summary'"
            )
        )
        assert held.scalar_one() == 4

        assert await repository.prune(now + timedelta(seconds=1), limit=100) >= 4
        assert mine(await repository.sessions(everyone, limit=50)) == []
        left = await session.execute(
            text("SELECT count(*) FROM agent_trace_sessions WHERE actor = 'usr_s'")
        )
        assert left.scalar_one() == 0
