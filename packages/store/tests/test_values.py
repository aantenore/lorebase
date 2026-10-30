"""The value index (revision 0045, `lorebase_store.values`): selection, trigger upkeep across
commits and overlays, lookups and rebuild."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, Record
from lorebase_core.revision import Author, Origin
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordDelete, RecordUpsert
from lorebase_store import values
from lorebase_store.commit import CommitResult, SchemaUpsert, commit, create_domain
from lorebase_store.layout import resolve_layout
from lorebase_store.materialize import BranchMode, create_branch
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings

from .support import DOMAIN, Clock, T, attribute, domain, ident

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
AUTHOR = Author(principal="usr_test")
ORIGIN = Origin(type="test")

PRODUCT = ident("knd", 50)
CODE = ident("att", 501)
COLOR = ident("att", 502)
SIZE = ident("att", 503)
NOTES = ident("att", 504)
TAGS = ident("att", 505)
COLORS = ("Rosso", "Blu", "Café crème")


def product_kind() -> Kind:
    attributes = [
        attribute(CODE, "code", T("string"), required=True),
        attribute(COLOR, "color", T("string")),
        attribute(SIZE, "size", TypeSpec.enum("S", "M", "L")),
        attribute(NOTES, "notes", T("text")),
        attribute(TAGS, "tags", TypeSpec.array(T("string")), resolvable=True),
    ]
    return Kind(
        id=PRODUCT,
        api_name="Product",
        description="Something sold.",
        primary_key=(CODE,),
        title_attribute=CODE,
        attributes={a.id: a for a in attributes},
    )


def product(number: int, *, color: str | None = None) -> RecordUpsert:
    record_values: dict[str, Any] = {
        CODE: f"P-{number:03d}",
        COLOR: color or COLORS[number % len(COLORS)],
        SIZE: "SML"[number % 3],
        NOTES: f"Product number {number}.",
        TAGS: ["new", "sale"] if number % 2 else ["new"],
    }
    return RecordUpsert(record=Record(kind=PRODUCT, key=f"P-{number:03d}", values=record_values))


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    return factory


async def run(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, changes: list[Any], **options: Any
) -> CommitResult:
    async with sessions() as session:
        return await commit(
            session,
            DOMAIN,
            options.pop("branch", "main"),
            changes,
            author=AUTHOR,
            message="test",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
            **options,
        )


async def held(sessions: Sessions, branch: str = "main") -> dict[tuple[str, str], int]:
    async with sessions() as session:
        rows = await session.execute(
            text(
                "SELECT attribute_id, value, records FROM mat_values "
                "WHERE domain_id = :d AND branch = :b"
            ),
            {"d": DOMAIN, "b": branch},
        )
        return {(a, v): int(n) for a, v, n in rows.all()}


async def sync(sessions: Sessions, settings: StoreSettings, **options: Any) -> Any:
    async with sessions() as session, session.begin():
        return await values.sync_selection(session, DOMAIN, settings=settings, **options)


def test_normalize_mirrors_the_database() -> None:
    assert values.normalize("  Café\tCRÈME ") == "cafe creme"
    assert values.normalize("Ärger") == "arger"


async def test_selection_upkeep_lookups_and_rebuild(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    settings = runtime.settings
    await run(sessions, runtime, clock, [], schema_changes=[SchemaUpsert(product_kind())])
    await run(sessions, runtime, clock, [product(n) for n in range(60)])

    report = await sync(sessions, settings)
    assert set(report.selected) == {(PRODUCT, COLOR), (PRODUCT, SIZE), (PRODUCT, TAGS)}
    assert report.backfilled > 0
    counts = await held(sessions)
    assert counts[(COLOR, "Rosso")] == 20
    assert counts[(SIZE, "S")] == 20
    assert counts[(TAGS, "new")] == 60
    assert counts[(TAGS, "sale")] == 30
    assert not any(attribute in (CODE, NOTES) for attribute, _ in counts)
    async with sessions() as session:
        norm = await session.scalar(
            text("SELECT norm FROM mat_values WHERE value = 'Café crème' LIMIT 1")
        )
    assert norm == values.normalize("Café crème")

    # Unchanged since the last pass: nothing to do.
    assert (await sync(sessions, settings)).skipped

    # Commits keep the counts: an update moves one, a delete spends one.
    await run(sessions, runtime, clock, [product(0, color="Giallo")])
    await run(sessions, runtime, clock, [RecordDelete(kind=PRODUCT, key="P-003")])
    counts = await held(sessions)
    assert counts[(COLOR, "Giallo")] == 1
    assert counts[(COLOR, "Rosso")] == 18
    await run(sessions, runtime, clock, [product(0)])
    assert (COLOR, "Giallo") not in await held(sessions)

    async with sessions() as session:
        await scope_to_domain(session, DOMAIN)
        layout = await resolve_layout(session, DOMAIN, "main")
        exact = await values.exact_values(session, layout, ["ROSSO", "cafe creme"], limit=10)
        assert {(h.term, h.value, h.records) for h in exact} == {
            ("ROSSO", "Rosso", 19),
            ("cafe creme", "Café crème", 20),
        }
        for definer in (True, False):
            reads = settings.reads.model_copy(update={"use_definer_functions": definer})
            similar = await values.similar_values(
                session,
                layout,
                ["rossi", "cafe crem"],
                threshold=0.3,
                per_term=3,
                runtime=StoreRuntime.from_settings(settings.model_copy(update={"reads": reads})),
            )
            best = {
                h.term: h.value
                for h in similar
                if h.similarity == max(x.similarity for x in similar if x.term == h.term)
            }
            assert best == {"rossi": "Rosso", "cafe crem": "Café crème"}, definer

    # The triggers and a recount agree.
    before = await held(sessions)
    async with sessions() as session, session.begin():
        await values.rebuild(session, DOMAIN, settings=settings)
    assert await held(sessions) == before


async def test_overlays_hold_their_own_values_and_count_live(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await run(sessions, runtime, clock, [], schema_changes=[SchemaUpsert(product_kind())])
    await run(sessions, runtime, clock, [product(n) for n in range(9)])
    await sync(sessions, runtime.settings)
    async with sessions() as session:
        await create_branch(
            session,
            DOMAIN,
            "agent/colors",
            created_by="usr_test",
            source="main",
            mode=BranchMode.OVERLAY,
            runtime=runtime,
        )
    await run(sessions, runtime, clock, [product(1, color="Viola")], branch="agent/colors")
    overlay = await held(sessions, "agent/colors")
    assert overlay[(COLOR, "Viola")] == 1

    async with sessions() as session:
        layout = await resolve_layout(session, DOMAIN, "agent/colors")
        assert layout.overlay
        exact = await values.exact_values(session, layout, ["viola", "blu"], limit=10)
        assert {h.value for h in exact} == {"Viola", "Blu"}
        live = await values.live_counts(session, layout, PRODUCT, COLOR, ["Blu", "Viola", "Rosso"])
        assert live == {"Blu": 2, "Viola": 1, "Rosso": 3}
        tags = await values.live_counts(session, layout, PRODUCT, TAGS, ["sale"], array=True)
        assert tags == {"sale": 4}


async def test_thresholds_and_switch_off(sessions: Sessions, runtime: StoreRuntime) -> None:
    clock = Clock()
    await run(sessions, runtime, clock, [], schema_changes=[SchemaUpsert(product_kind())])
    await run(sessions, runtime, clock, [product(n) for n in range(60)])
    narrow = runtime.settings.values.model_copy(update={"max_distinct": 2})
    settings = runtime.settings.model_copy(update={"values": narrow})
    report = await sync(sessions, settings)
    assert (PRODUCT, COLOR) not in report.selected  # three colors
    assert (PRODUCT, SIZE) in report.selected  # enums are always held

    off = runtime.settings.values.model_copy(update={"enabled": False})
    report = await sync(sessions, runtime.settings.model_copy(update={"values": off}))
    assert report.selected == []
    assert await held(sessions) == {}
