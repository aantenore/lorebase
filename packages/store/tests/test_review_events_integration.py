"""Proposal and Conflict events against Postgres (design 10.5): written in the transaction of
the change, compared with the previous attempt (a recompute carries resolutions over without
events), and delivered only to webhooks that may read the Attribute and both branches."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Record, Sensitivity, new_ulid
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordUpsert
from lorebase_store.auth.scope import rls_scope
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.materialize import create_branch
from lorebase_store.merging import MergeRequest, MergeService
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus
from lorebase_store.outbox_events import EVENT_TYPES
from lorebase_store.repositories import ProposalRepository, ProposalRow, scope_to_domain
from lorebase_store.repositories.merges import ResolutionRepository, ResolutionRow
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings
from lorebase_store.webhooks import (
    SigningKeys,
    WebhookRepository,
    WebhookSettings,
    deliver_pending,
    signing_key,
)

from .support import (
    AUTHOR,
    COUNTRY,
    DOMAIN,
    LEGAL_NAME,
    ORIGIN,
    SUPPLIER,
    SUPPLIER_CODE,
    Clock,
    attribute,
    domain,
    ident,
    supplier_kind,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
NOTES = ident("att", 190)
SIDE = "side"
SECRET_NOTE = "side-secret-note"
REVIEW = ["proposal.*", "conflict.*"]


def supplier(code: str, *, country: str, notes: str | None = None) -> RecordUpsert:
    values: dict[str, Any] = {SUPPLIER_CODE: code, LEGAL_NAME: code, COUNTRY: country}
    if notes is not None:
        values[NOTES] = notes
    return RecordUpsert(record=Record(kind=SUPPLIER, key=code, values=values))


async def write(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, branch: str, changes: list[Any]
) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            branch,
            changes,
            author=AUTHOR,
            message="test",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
        )


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    """`main` and `side` both changed S-1's country and its PII notes since they forked."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    kind = supplier_kind()
    notes = attribute(NOTES, "notes", TypeSpec.of("string"), sensitivity=(Sensitivity.PII,))
    kind = kind.model_copy(update={"attributes": {**kind.attributes, NOTES: notes}})
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=clock)
    async with factory() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-1", country="IT", notes="n0"), supplier("S-2", country="IT")],
            author=AUTHOR,
            message="Schema",
            origin=ORIGIN,
            schema_changes=[SchemaUpsert(kind)],
            runtime=runtime,
            clock=clock,
        )
    async with factory() as session:
        await create_branch(
            session, DOMAIN, SIDE, created_by=AUTHOR.principal, runtime=runtime, clock=clock
        )
    await write(factory, runtime, clock, "main", [supplier("S-1", country="FR", notes="n1")])
    await write(factory, runtime, clock, SIDE, [supplier("S-1", country="DE", notes=SECRET_NOTE)])
    return factory


async def latest(sessions: Sessions) -> int:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        found = await session.scalar(text("SELECT coalesce(max(id), 0) FROM outbox"))
        return int(found)


async def events(sessions: Sessions, after: int) -> list[tuple[str, dict[str, Any]]]:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        rows = await session.execute(
            text("SELECT event_type, payload FROM outbox WHERE id > :after ORDER BY id"),
            {"after": after},
        )
        return [(str(kind), dict(payload)) for kind, payload in rows]


def new_proposal(clock: Clock) -> ProposalRow:
    now = clock()
    return ProposalRow(
        id=f"prp_{new_ulid()}",
        title="Merge side",
        description=None,
        source_ref=SIDE,
        target_ref="main",
        origin=ProposalOrigin.MANUAL,
        status=ProposalStatus.OPEN,
        author=AUTHOR.principal,
        created_at=now,
        checks={},
        policies={},
        updated_at=now,
    )


async def open_proposal(sessions: Sessions, clock: Clock) -> ProposalRow:
    proposal = new_proposal(clock)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await ProposalRepository(session, DOMAIN).insert(proposal)
    return proposal


async def attempt(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, proposal: ProposalRow, *, merge: bool
) -> Any:
    async with sessions() as session, session.begin():
        service = MergeService(session, DOMAIN, runtime=runtime, clock=clock)
        request = MergeRequest(proposal=proposal, actor=AUTHOR.principal)
        return await (service.merge(request) if merge else service.recompute(request))


async def resolve(
    sessions: Sessions, clock: Clock, proposal: ProposalRow, fingerprint: str, choice: str
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await ResolutionRepository(session, DOMAIN).upsert_many(
            proposal.id,
            [
                ResolutionRow(
                    fingerprint=fingerprint,
                    resolution={"fingerprint": fingerprint, "choice": choice},
                    resolved_by="usr_reviewer",
                    at=clock(),
                )
            ],
        )


def by_attribute(found: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {payload["attribute"]: payload for _, payload in found}


async def test_the_lifecycle_of_a_proposal_and_its_conflicts(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    mark = await latest(sessions)
    proposal = await open_proposal(sessions, clock)
    [(kind, opened)] = await events(sessions, mark)
    assert kind == "proposal.opened"
    assert opened["proposal"] == proposal.id
    assert (opened["branch"], opened["source_branch"], opened["status"]) == ("main", SIDE, "open")
    assert "title" not in opened

    mark = await latest(sessions)
    first = await attempt(sessions, runtime, clock, proposal, merge=False)
    found = await events(sessions, mark)
    assert {kind for kind, _ in found} == {"conflict.opened"}
    conflicts = by_attribute(found)
    assert set(conflicts) == {"country", "notes"}
    country, notes = conflicts["country"], conflicts["notes"]
    assert (country["code"], country["kind"], country["key"]) == ("DV1", "Supplier", "S-1")
    assert country["attempt"] == first.attempt.id
    assert country["$audience"] == {"branches": ["main", SIDE], "tags": []}
    assert notes["$audience"]["tags"] == ["pii"]
    assert SECRET_NOTE not in json.dumps(found)

    # Recomputing what did not change writes nothing.
    mark = await latest(sessions)
    await attempt(sessions, runtime, clock, proposal, merge=False)
    assert await events(sessions, mark) == []

    # A resolution applied: that Conflict only.
    await resolve(sessions, clock, proposal, country["conflict"], "theirs")
    mark = await latest(sessions)
    await attempt(sessions, runtime, clock, proposal, merge=False)
    [(kind, resolved)] = await events(sessions, mark)
    assert kind == "conflict.resolved"
    assert (resolved["conflict"], resolved["reason"]) == (country["conflict"], "resolution")
    assert (resolved["choice"], resolved["resolved_by"]) == ("theirs", "usr_reviewer")

    # The source moves: the stale attempt is recomputed, the resolution is carried over by
    # fingerprint and the open Conflict stays open. No event repeats.
    await write(sessions, runtime, clock, SIDE, [supplier("S-2", country="ES")])
    mark = await latest(sessions)
    moved = await attempt(sessions, runtime, clock, proposal, merge=False)
    assert moved.stale_previous
    assert moved.attempt.status.value == "conflicted"
    assert [k for k, _ in await events(sessions, mark) if k.startswith("conflict.")] == []

    # The last resolution and the merge, in the order they happened.
    await resolve(sessions, clock, proposal, notes["conflict"], "ours")
    mark = await latest(sessions)
    merged = await attempt(sessions, runtime, clock, proposal, merge=True)
    assert merged.merged
    found = await events(sessions, mark)
    assert [kind for kind, _ in found] == [
        "conflict.resolved",
        "revision.committed",
        "proposal.merged",
    ]
    assert found[0][1]["conflict"] == notes["conflict"]
    assert found[2][1]["revision"] == merged.head.hex()


async def test_rolled_back_work_writes_no_event(sessions: Sessions, runtime: StoreRuntime) -> None:
    clock = Clock()
    mark = await latest(sessions)
    proposal = new_proposal(clock)

    async def open_attempt_and_fail() -> None:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await ProposalRepository(session, DOMAIN).insert(proposal)
            result = await MergeService(session, DOMAIN, runtime=runtime, clock=clock).recompute(
                MergeRequest(proposal=proposal, actor=AUTHOR.principal)
            )
            assert result.computation is not None
            assert result.computation.open_count == 2  # the events were written, uncommitted
            raise RuntimeError("the request failed after the attempt")

    with pytest.raises(RuntimeError):
        await open_attempt_and_fail()
    assert await events(sessions, mark) == []


async def test_updates_and_close(sessions: Sessions) -> None:
    clock = Clock()
    proposal = await open_proposal(sessions, clock)
    mark = await latest(sessions)
    async with sessions() as session, session.begin():
        repository = ProposalRepository(session, DOMAIN)
        await scope_to_domain(session, DOMAIN)
        await repository.update(proposal.id, at=clock(), title="New title")
        await repository.update(proposal.id, at=clock(), checks={"x": 1})  # not an update
        await repository.update(proposal.id, at=clock(), status=ProposalStatus.CLOSED)
        await repository.update(proposal.id, at=clock(), status=ProposalStatus.CLOSED)  # again
    found = await events(sessions, mark)
    assert [kind for kind, _ in found] == ["proposal.updated", "proposal.closed"]
    assert found[0][1]["changed"] == ["title"]
    assert "New title" not in json.dumps(found)
    assert found[1][1]["status"] == "closed"


class Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        self.sent.append((url.split("/")[2], json.loads(body)))
        return 204


async def public(host: str) -> Sequence[str]:
    return ["93.184.215.14"]


async def add_webhook(
    sessions: Sessions,
    host: str,
    events: Sequence[str],
    *,
    branches: Sequence[str] | None = None,
    clearances: Sequence[str] = (),
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await WebhookRepository(session, DOMAIN).add(
            url=f"https://{host}/hook",
            description=None,
            event_types=list(events),
            branches=branches,
            signing_key=SigningKeys(None).store(signing_key("secret")),
            created_by="usr_admin",
            clearances=clearances,
        )


async def test_webhooks_receive_only_what_they_may_read(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await add_webhook(sessions, "plain.example.com", REVIEW)
    await add_webhook(sessions, "cleared.example.com", REVIEW, clearances=["pii"])
    await add_webhook(sessions, "main-only.example.com", REVIEW, branches=["main"])
    await add_webhook(sessions, "both.example.com", REVIEW, branches=["main", SIDE])
    await add_webhook(sessions, "default.example.com", EVENT_TYPES)
    proposal = await open_proposal(sessions, clock)
    await attempt(sessions, runtime, clock, proposal, merge=False)
    send = Sender()
    report = await deliver_pending(
        sessions,
        WebhookSettings(),
        send,
        writes=StoreSettings().writes,
        resolve=public,
    )
    assert report.delivered == len(send.sent)
    got: dict[str, list[dict[str, Any]]] = {}
    for host, body in send.sent:
        got.setdefault(host, []).append(body)

    def described(host: str) -> list[tuple[str, str | None]]:
        return sorted((b["type"], b.get("attribute")) for b in got.get(host, []))

    everything = [
        ("conflict.opened", "country"),
        ("conflict.opened", "notes"),
        ("proposal.opened", None),
    ]
    assert described("cleared.example.com") == everything
    # Not cleared for PII: the Conflict on `notes` does not exist for it.
    assert described("plain.example.com") == [
        ("conflict.opened", "country"),
        ("proposal.opened", None),
    ]
    assert described("both.example.com") == described("plain.example.com")
    # Reads `main` only: nothing about a Proposal from `side`.
    assert "main-only.example.com" not in got
    # The default subscription is unchanged: no Proposal or Conflict events.
    assert "default.example.com" not in got
    for host in ("plain.example.com", "both.example.com"):
        text_sent = json.dumps(got[host])
        assert "notes" not in text_sent
        assert NOTES not in text_sent
    for bodies in got.values():
        assert all("$audience" not in body and "sequence" in body for body in bodies)
