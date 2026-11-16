"""Proposal and Conflict events against Postgres (design 10.5): written in the transaction of
the change, compared with the previous attempt (a recompute carries resolutions over without
events), and delivered only to webhooks that may read the Attribute and both branches."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Record, Sensitivity, new_ulid
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordUpsert
from lorebase_store.auth.scope import rls_scope
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.commit.errors import StaleHeadError
from lorebase_store.materialize import create_branch
from lorebase_store.merging import MergeRequest, MergeService
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus, ReviewVerdict
from lorebase_store.outbox_events import EVENT_TYPES
from lorebase_store.repositories import (
    ProposalRepository,
    ProposalRow,
    ReviewRepository,
    ReviewRow,
    scope_to_domain,
)
from lorebase_store.repositories.merges import ResolutionRepository, ResolutionRow
from lorebase_store.repositories.refs import RefMovedError, RefRepository
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
    wait_for_lock_waiter,
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


def new_proposal(clock: Clock, source: str = SIDE) -> ProposalRow:
    now = clock()
    return ProposalRow(
        id=f"prp_{new_ulid()}",
        title="Merge side",
        description=None,
        source_ref=source,
        target_ref="main",
        origin=ProposalOrigin.MANUAL,
        status=ProposalStatus.OPEN,
        author=AUTHOR.principal,
        created_at=now,
        checks={},
        policies={},
        updated_at=now,
    )


async def open_proposal(sessions: Sessions, clock: Clock, source: str = SIDE) -> ProposalRow:
    proposal = new_proposal(clock, source)
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
    await add_webhook(sessions, "review.example.com", REVIEW)
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
    await add_webhook(sessions, "review.example.com", REVIEW)
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
        # The same values again (a Source sync re-run with nothing new): not an update.
        await repository.update(
            proposal.id, at=clock(), title="Merge side", description=None, policies={}
        )
        await repository.update(
            proposal.id, at=clock(), title="New title", description=None, policies={}
        )
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


async def test_no_conflict_event_without_a_subscriber(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await add_webhook(sessions, "proposals.example.com", ["proposal.*"])
    proposal = await open_proposal(sessions, clock)
    mark = await latest(sessions)
    result = await attempt(sessions, runtime, clock, proposal, merge=False)
    assert result.computation.open_count == 2
    assert await events(sessions, mark) == []


async def test_events_past_the_cap_are_summarized(sessions: Sessions, store: StoreSettings) -> None:
    capped = StoreRuntime.from_settings(store, webhooks=WebhookSettings(conflict_events_max=1))
    clock = Clock()
    await add_webhook(sessions, "review.example.com", REVIEW)
    proposal = await open_proposal(sessions, clock)
    mark = await latest(sessions)
    await attempt(sessions, capped, clock, proposal, merge=False)
    found = await events(sessions, mark)
    assert [kind for kind, _ in found] == ["conflict.opened", "conflict.opened"]
    first, summary = (payload for _, payload in found)
    assert "conflict" in first
    assert (summary["count"], summary["truncated"]) == (2, True)
    assert "conflict" not in summary
    assert summary["$audience"]["tags"] == ["pii"]  # it counts the Conflict on `notes`


async def test_concurrent_recomputes_write_one_set_of_events(
    sessions: Sessions, runtime: StoreRuntime, engine: AsyncEngine
) -> None:
    clock = Clock()
    await add_webhook(sessions, "review.example.com", REVIEW)
    proposal = await open_proposal(sessions, clock)
    mark = await latest(sessions)
    request = MergeRequest(proposal=proposal, actor=AUTHOR.principal)
    async with sessions() as first:
        await first.begin()
        await MergeService(first, DOMAIN, runtime=runtime, clock=clock).current(request)

        async def second() -> Any:
            async with sessions() as session, session.begin():
                service = MergeService(session, DOMAIN, runtime=runtime, clock=clock)
                return await service.current(request)

        waiting = asyncio.create_task(second())
        await wait_for_lock_waiter(
            engine, waiting, locktype="transactionid", what="the second recompute"
        )
        await first.commit()
    other = await waiting
    assert other.computation is None  # it found the attempt the first one stored
    found = await events(sessions, mark)
    assert sorted(kind for kind, _ in found) == ["conflict.opened", "conflict.opened"]


async def test_a_failed_merge_commit_leaves_no_event(
    sessions: Sessions, runtime: StoreRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    await add_webhook(sessions, "review.example.com", REVIEW)
    proposal = await open_proposal(sessions, clock)
    first = await attempt(sessions, runtime, clock, proposal, merge=False)
    assert first.computation is not None
    for state in first.computation.conflicts:
        await resolve(sessions, clock, proposal, state.conflict.fingerprint, "ours")
    mark = await latest(sessions)

    async def moved(self: RefRepository, name: str, **_: Any) -> int:
        raise RefMovedError(DOMAIN, name, b"\0", 0)

    monkeypatch.setattr(RefRepository, "compare_and_swap", moved)
    with pytest.raises(StaleHeadError):
        await attempt(sessions, runtime, clock, proposal, merge=True)
    assert await events(sessions, mark) == []  # the conflict.resolved rolled back too


async def test_fast_forward_and_up_to_date_merges(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await add_webhook(sessions, "review.example.com", REVIEW)
    async with sessions() as session:
        for name in ("ahead", "same"):
            await create_branch(
                session, DOMAIN, name, created_by=AUTHOR.principal, runtime=runtime, clock=clock
            )
    await write(sessions, runtime, clock, "ahead", [supplier("S-3", country="NL")])
    for source, expected in (
        ("ahead", ["revision.committed", "proposal.merged"]),
        ("same", ["proposal.merged"]),
    ):
        proposal = await open_proposal(sessions, clock, source)
        mark = await latest(sessions)
        merged = await attempt(sessions, runtime, clock, proposal, merge=True)
        assert merged.merged
        found = await events(sessions, mark)
        assert [kind for kind, _ in found] == expected, source
        assert found[-1][1]["revision"] == merged.head.hex()


class Flaky(Sender):
    """Answers 500 to every request about `notes`, 204 to the others."""

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        await super().__call__(url, body, headers, timeout_s, addresses)
        return 500 if json.loads(body).get("attribute") == "notes" else 204


async def test_a_clearance_narrowed_before_the_claim_withholds_the_event(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await add_webhook(sessions, "cleared.example.com", ["conflict.opened"], clearances=["pii"])
    proposal = await open_proposal(sessions, clock)
    await attempt(sessions, runtime, clock, proposal, merge=False)
    settings = WebhookSettings(backoff_base_s=1, backoff_max_s=1)
    send = Flaky()
    now = datetime.now(UTC)
    first = await deliver_pending(
        sessions, settings, send, writes=StoreSettings().writes, resolve=public, clock=lambda: now
    )
    assert (first.attempted, first.delivered, first.retrying) == (2, 1, 1)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        hooks = WebhookRepository(session, DOMAIN)
        [hook] = await hooks.all()
        await hooks.set_clearances(hook.id, [])
    later = now + timedelta(minutes=5)
    second = await deliver_pending(
        sessions, settings, send, writes=StoreSettings().writes, resolve=public, clock=lambda: later
    )
    assert second.attempted == 0  # the retry of the PII Conflict is withheld
    assert len(send.sent) == 2
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        found = await session.execute(
            text(
                "SELECT payload->>'attribute', status, last_error FROM webhook_deliveries "
                "WHERE domain_id = :d"
            ),
            {"d": DOMAIN},
        )
        statuses = {attribute: (status, error) for attribute, status, error in found}
    assert statuses["country"][0] == "delivered"
    assert statuses["notes"][0] == "dead"
    assert statuses["notes"][1].startswith("withdrawn")
    for _, body in send.sent:
        assert "$audience" not in body


async def attempts_of(sessions: Sessions, proposal: ProposalRow) -> list[tuple[str, bytes]]:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        found = await session.execute(
            text("SELECT status, theirs FROM merge_attempts WHERE proposal_id = :p"),
            {"p": proposal.id},
        )
        return [(str(status), bytes(theirs)) for status, theirs in found]


async def source_head(sessions: Sessions, branch: str) -> bytes:
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        found = await session.scalar(
            text("SELECT head FROM refs WHERE domain_id = :d AND name = :b"),
            {"d": DOMAIN, "b": branch},
        )
        return bytes(found)


async def queued_current(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, proposal: ProposalRow
) -> Any:
    async with sessions() as session, session.begin():
        service = MergeService(session, DOMAIN, runtime=runtime, clock=clock)
        return await service.current(MergeRequest(proposal=proposal, actor=AUTHOR.principal))


async def test_a_read_queued_behind_a_merge_adds_nothing(
    sessions: Sessions, runtime: StoreRuntime, engine: AsyncEngine
) -> None:
    clock = Clock()
    await add_webhook(sessions, "review.example.com", REVIEW)
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "ahead", created_by=AUTHOR.principal, runtime=runtime, clock=clock
        )
    await write(sessions, runtime, clock, "ahead", [supplier("S-3", country="NL")])
    proposal = await open_proposal(sessions, clock, "ahead")
    async with sessions() as merging:
        await merging.begin()
        service = MergeService(merging, DOMAIN, runtime=runtime, clock=clock)
        merged = await service.merge(MergeRequest(proposal=proposal, actor=AUTHOR.principal))
        assert merged.merged
        reading = asyncio.create_task(queued_current(sessions, runtime, clock, proposal))
        await wait_for_lock_waiter(engine, reading, locktype="transactionid", what="the read")
        await merging.commit()
    mark = await latest(sessions)
    found = await reading
    assert found.computation is None  # nothing recomputed on the merged Proposal
    assert found.attempt.id == merged.attempt.id
    assert [status for status, _ in await attempts_of(sessions, proposal)] == ["merged"]
    assert await events(sessions, mark) == []


async def test_a_head_moved_while_waiting_is_the_one_computed(
    sessions: Sessions, runtime: StoreRuntime, engine: AsyncEngine
) -> None:
    clock = Clock()
    await add_webhook(sessions, "review.example.com", REVIEW)
    proposal = await open_proposal(sessions, clock)
    await attempt(sessions, runtime, clock, proposal, merge=False)
    await write(sessions, runtime, clock, SIDE, [supplier("S-2", country="ES")])  # stale now
    mark = await latest(sessions)
    async with sessions() as holder:
        await holder.begin()
        await scope_to_domain(holder, DOMAIN)
        # Keeper's `FOR UPDATE` excludes the run's `FOR NO KEY UPDATE`.
        await ProposalRepository(holder, DOMAIN).get(proposal.id, for_update=True)
        reading = asyncio.create_task(queued_current(sessions, runtime, clock, proposal))
        await wait_for_lock_waiter(engine, reading, locktype="transactionid", what="the read")
        await write(sessions, runtime, clock, SIDE, [supplier("S-2", country="PT")])
        await holder.commit()
    found = await reading
    moved = await source_head(sessions, SIDE)
    assert found.computation is not None
    assert found.attempt.theirs == moved
    heads = {theirs for status, theirs in await attempts_of(sessions, proposal)}
    assert moved in heads
    assert len(heads) == 2  # the first attempt and the one at the new head, none between
    announced = [kind for kind, _ in await events(sessions, mark) if kind.startswith("conflict.")]
    assert announced == []  # same Conflicts at the new head: nothing to announce


async def test_the_run_lock_lets_reviews_in(sessions: Sessions, runtime: StoreRuntime) -> None:
    clock = Clock()
    proposal = await open_proposal(sessions, clock)
    async with sessions() as running:
        await running.begin()
        service = MergeService(running, DOMAIN, runtime=runtime, clock=clock)
        await service.recompute(MergeRequest(proposal=proposal, actor=AUTHOR.principal))

        async def review() -> None:
            async with sessions() as session, session.begin():
                await scope_to_domain(session, DOMAIN)
                await ReviewRepository(session, DOMAIN).insert(
                    ReviewRow(
                        id=f"rvw_{new_ulid()}",
                        proposal_id=proposal.id,
                        reviewer="usr_reviewer",
                        verdict=ReviewVerdict.COMMENT,
                        comment=None,
                        at=clock(),
                    )
                )

        await asyncio.wait_for(review(), timeout=10)  # not queued behind the run
        await running.commit()
