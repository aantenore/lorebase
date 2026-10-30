"""The value index (revision 0045, `lorebase_store.values`): selection, trigger upkeep across
commits and overlays, lookups and rebuild."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Domain, Kind, Record, Sensitivity
from lorebase_core.revision import Author, Origin
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordDelete, RecordUpsert
from lorebase_store import migrations, values
from lorebase_store.commit import CommitResult, SchemaUpsert, commit, create_domain
from lorebase_store.layout import resolve_layout
from lorebase_store.materialize import BranchMode, create_branch, delete_branch, promote
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .support import DOMAIN, Clock, T, attribute, domain, ident, wait_for_lock_waiter

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


def test_normalize_folds_case_accents_and_blanks() -> None:
    assert values.normalize("  Café\tCRÈME ") == "cafe creme"
    assert values.normalize("Ärger") == "arger"
    assert values.normalize("ΟΔΟΣ") == "οδοσ"


PARITY = [
    "  Café\tCRÈME ",
    "ΟΔΟΣ ΣΟΦΙΑΣ",
    f"a{chr(0xA0)}b{chr(0x2007)}c{chr(0x202F)}d{chr(0x1C)}e{chr(0x3000)}f",
    "Straße",
    "".join(chr(c) for c in (0x41C, 0x41E, 0x421, 0x41A, 0x412, 0x410)),  # Moscow, Cyrillic
    "हिन्दी",
    "ภาษาไทย",
    "שָׁלוֹם",
    "العربية",
    "Rock & Roll",
    "C++",
    "S/M",
    "50%",
]


async def test_normalize_matches_lb_value_norm(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT lb_value_norm(x) FROM unnest(CAST(:xs AS text[])) "
                "WITH ORDINALITY AS t(x, n) ORDER BY n"
            ),
            {"xs": PARITY},
        )
        assert list(found.scalars()) == [values.normalize(x) for x in PARITY]


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
        exact = await values.exact_values(session, layout, ["ROSSO", "cafe creme"], per_term=10)
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
    report = await values.run_value_index(sessions, DOMAIN, settings=settings, rebuild_all=True)
    assert report.backfilled == len(before)
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
        exact = await values.exact_values(session, layout, ["viola", "blu"], per_term=10)
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


RECOUNT = text(
    "SELECT m.branch, a.attribute_id, v.v, count(*) FROM mat_records m "
    "JOIN refs r ON r.domain_id = m.domain_id AND r.name = m.branch "
    "JOIN mat_value_attributes a ON a.domain_id = m.domain_id AND a.kind_id = m.kind_id "
    'CROSS JOIN LATERAL lb_value_strings(m."values" -> a.attribute_id, a.max_chars) AS v(v) '
    "WHERE m.domain_id = :d AND NOT m.deleted GROUP BY 1, 2, 3"
)
INDEXED = text("SELECT branch, attribute_id, value, records FROM mat_values WHERE domain_id = :d")
SIZES = text(
    "SELECT branch, attribute_id, size FROM mat_value_sizes WHERE domain_id = :d AND size <> 0"
)


async def agree(sessions: Sessions) -> dict[tuple[str, str, str], int]:
    """The index equals a recount of the Records; returns it."""
    async with sessions() as session:
        recount = {
            (b, a, v): int(n) for b, a, v, n in await session.execute(RECOUNT, {"d": DOMAIN})
        }
        index = {(b, a, v): int(n) for b, a, v, n in await session.execute(INDEXED, {"d": DOMAIN})}
        sizes = {(b, a): int(n) for b, a, n in await session.execute(SIZES, {"d": DOMAIN})}
    assert index == recount
    counted: dict[tuple[str, str], int] = {}
    for b, a, _ in index:
        counted[(b, a)] = counted.get((b, a), 0) + 1
    assert sizes == counted
    return index


async def seeded(sessions: Sessions, runtime: StoreRuntime, clock: Clock, count: int = 30) -> None:
    await run(sessions, runtime, clock, [], schema_changes=[SchemaUpsert(product_kind())])
    await run(sessions, runtime, clock, [product(n) for n in range(count)])


def tuned(runtime: StoreRuntime, **changes: Any) -> StoreSettings:
    return runtime.settings.model_copy(
        update={"values": runtime.settings.values.model_copy(update=changes)}
    )


async def test_reselection_and_max_chars_changes_recount(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    await sync(sessions, runtime.settings)
    await agree(sessions)
    dropped = await sync(sessions, tuned(runtime, max_distinct=2), force=True)
    assert (PRODUCT, COLOR) in dropped.removed
    assert all(a != COLOR for _, a, _ in await agree(sessions))
    # Values written while it was out are counted when it comes back; no key collides.
    await run(sessions, runtime, clock, [product(1, color="Giallo")])
    back = await sync(sessions, runtime.settings, force=True)
    assert (PRODUCT, COLOR) in back.added
    assert ("main", COLOR, "Giallo") in await agree(sessions)
    short = await sync(sessions, tuned(runtime, max_value_chars=5), force=True)
    # Every held Attribute is recounted (codes too: 30 Records are below ratio_min_records).
    assert {(PRODUCT, COLOR), (PRODUCT, SIZE), (PRODUCT, TAGS)} <= set(short.added)
    index = await agree(sessions)
    assert ("main", COLOR, "Café crème") not in index
    assert ("main", COLOR, "Rosso") in index


async def test_hysteresis_and_pass_limits(sessions: Sessions, runtime: StoreRuntime) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock, count=60)
    # Three colors: held at max_distinct 3, kept at 2 (2 * 1.5 >= 3), dropped at 2 without it.
    assert (PRODUCT, COLOR) in (await sync(sessions, tuned(runtime, max_distinct=3))).selected
    kept = await sync(sessions, tuned(runtime, max_distinct=2, hysteresis=1.5), force=True)
    assert (PRODUCT, COLOR) in kept.selected
    gone = await sync(sessions, tuned(runtime, max_distinct=2, hysteresis=1.0), force=True)
    assert (PRODUCT, COLOR) not in gone.selected
    # A pass adds at most max_added_per_pass Attributes, the next one the rest.
    off = await sync(sessions, tuned(runtime, enabled=False), force=True)
    assert off.selected == []
    first = await values.run_value_index(
        sessions, DOMAIN, settings=tuned(runtime, max_added_per_pass=1)
    )
    assert len(first.added) == 1
    assert len(first.pending) == 2
    async with sessions() as session:
        queued = await session.scalar(
            text("SELECT count(*) FROM jobs WHERE type = 'value_index' AND domain_id = :d"),
            {"d": DOMAIN},
        )
    assert queued == 1
    rest = await values.run_value_index(sessions, DOMAIN, settings=runtime.settings)
    assert len(rest.added) == 2
    await agree(sessions)
    # A branch holding too many values of an Attribute drops it, enums included.
    huge = await sync(sessions, tuned(runtime, hard_max_distinct=2), force=False)
    assert (PRODUCT, SIZE) in huge.removed


async def test_a_write_past_the_limit_is_never_served_and_the_pass_drops_it(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    three = tuned(runtime, max_distinct=3, hysteresis=1.0)
    assert (PRODUCT, COLOR) in (await sync(sessions, three)).added
    await agree(sessions)
    # A fourth color takes main past max_values (3): marked at once, no longer read.
    await run(sessions, runtime, clock, [product(1, color="Giallo")])
    async with sessions() as session:
        await scope_to_domain(session, DOMAIN)
        rows = await values.held_selection(session, DOMAIN)
        assert rows[(PRODUCT, COLOR)] == values.Held(64, 3, overflowed=True)
        assert (PRODUCT, COLOR) not in await values.selection(session, DOMAIN)
        layout = await resolve_layout(session, DOMAIN, "main")
        exact = await values.exact_values(session, layout, ["rosso", "giallo"], per_term=5)
        assert exact == []
        for definer in (True, False):
            reads = runtime.settings.reads.model_copy(update={"use_definer_functions": definer})
            similar = await values.similar_values(
                session,
                layout,
                ["rossi"],
                threshold=0.3,
                per_term=3,
                runtime=StoreRuntime.from_settings(
                    runtime.settings.model_copy(update={"reads": reads})
                ),
            )
            assert all(h.attribute_id != COLOR for h in similar), definer
    # Writes stop counting it: the table does not grow until the pass drops it.
    await run(sessions, runtime, clock, [product(2, color="Viola")])
    assert (COLOR, "Viola") not in await held(sessions)
    report = await sync(sessions, three)
    assert (PRODUCT, COLOR) in report.removed
    assert all(a != COLOR for _, a, _ in await agree(sessions))


async def test_a_new_limit_applies_without_a_recount(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    await sync(sessions, tuned(runtime, max_distinct=3, hysteresis=1.0))
    report = await sync(sessions, tuned(runtime, max_distinct=3, hysteresis=2.0))
    assert report.added == []
    assert report.removed == []
    async with sessions() as session:
        rows = await values.held_selection(session, DOMAIN)
    assert rows[(PRODUCT, COLOR)].max_values == 6
    assert rows[(PRODUCT, SIZE)].max_values == runtime.settings.values.hard_max_distinct
    await run(sessions, runtime, clock, [product(1, color="Giallo")])
    assert ("main", COLOR, "Giallo") in await agree(sessions)


async def test_a_schema_change_applies_the_new_limit_in_the_same_commit(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    one = tuned(runtime, max_distinct=1, hysteresis=1.0)
    report = await sync(sessions, one)
    assert (PRODUCT, TAGS) in report.added  # resolvable: true, held whatever its values
    # Tags become a plain string array: limit 1, and main already holds two tags.
    kind = product_kind()
    tags = kind.attributes[TAGS].model_copy(update={"resolvable": None})
    plain = kind.model_copy(update={"attributes": {**kind.attributes, TAGS: tags}})
    await run(
        sessions,
        StoreRuntime.from_settings(one),
        clock,
        [],
        schema_changes=[SchemaUpsert(plain)],
    )
    async with sessions() as session:
        await scope_to_domain(session, DOMAIN)
        rows = await values.held_selection(session, DOMAIN)
        assert rows[(PRODUCT, TAGS)] == values.Held(64, 1, overflowed=True)
        assert rows[(PRODUCT, SIZE)].max_values == one.values.hard_max_distinct
        assert (PRODUCT, TAGS) not in await values.selection(session, DOMAIN)
        layout = await resolve_layout(session, DOMAIN, "main")
        assert await values.exact_values(session, layout, ["sale"], per_term=5) == []
    assert (PRODUCT, TAGS) in (await sync(sessions, one)).removed


async def test_an_upgraded_install_counts_sizes_and_decides_limits_again(
    engine: AsyncEngine,
    sessions: Sessions,
    runtime: StoreRuntime,
    database_url: str,
    database: DatabaseSettings,
    store: StoreSettings,
) -> None:
    """Revision 0047 on an index of revision 0045: sizes counted, every held Attribute at
    `hard_max_distinct` until the next pass (cursors cleared) sets its own limit."""
    clock = Clock()
    await seeded(sessions, runtime, clock)
    await sync(sessions, runtime.settings)
    before = await agree(sessions)
    await engine.dispose()
    await asyncio.to_thread(
        migrations.downgrade, "0046", database=database, store=store, url=database_url
    )
    await asyncio.to_thread(migrations.upgrade, database=database, store=store, url=database_url)
    assert await agree(sessions) == before
    async with sessions() as session, session.begin():
        rows = await values.held_selection(session, DOMAIN)
        cursors = await session.scalar(text("SELECT count(*) FROM mat_value_cursors"))
        # Writers of the previous trigger body during a rolling upgrade moved the sizes.
        await session.execute(text("UPDATE mat_value_sizes SET size = size + 7"))
    assert {row.max_values for row in rows.values()} == {store.values.hard_max_distinct}
    assert cursors == 0
    # The first pass without a cursor counts the sizes again before deciding.
    report = await sync(sessions, runtime.settings)
    assert not report.skipped
    assert await agree(sessions) == before
    async with sessions() as session:
        rows = await values.held_selection(session, DOMAIN)
    plain = int(store.values.max_distinct * store.values.hysteresis)
    assert rows[(PRODUCT, COLOR)].max_values == plain
    assert rows[(PRODUCT, SIZE)].max_values == store.values.hard_max_distinct


OWNER = "lb_test_migration_owner"


async def test_the_upgrade_rewrites_every_domain_under_forced_row_level_security(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime, store: StoreSettings
) -> None:
    """Revision 0047's data statements, run as a role that is neither superuser nor exempt
    from row-level security (as a table owner is under FORCE): they set the bypass themselves."""
    clock = Clock()
    await seeded(sessions, runtime, clock)
    await sync(sessions, runtime.settings)
    before = await agree(sessions)
    tables = "mat_value_attributes, mat_value_sizes, mat_values, mat_value_cursors, resolve_misses"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{OWNER}') "
                f"THEN CREATE ROLE {OWNER} NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$"
            )
        )
        await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {OWNER}"))
        await connection.execute(text(f"GRANT ALL ON {tables} TO {OWNER}"))
        await connection.execute(
            text(
                "INSERT INTO resolve_misses (domain_id, branch, phrase, misses, callers, "
                "first_seen, last_seen) VALUES (:d, 'main', 'rosy', 2, ARRAY['aa', 'bb'], "
                "now(), now())"
            ),
            {"d": DOMAIN},
        )
        await connection.execute(text("UPDATE mat_value_attributes SET max_values = 1"))
        await connection.execute(text("DELETE FROM mat_value_sizes"))
    module = ScriptDirectory.from_config(migrations.alembic_config()).get_revision("0047").module
    limit = store.values.hard_max_distinct
    async with engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {OWNER}"))
        await connection.run_sync(lambda sync_connection: module._data(sync_connection, limit))
    assert await agree(sessions) == before
    async with sessions() as session:
        rows = await values.held_selection(session, DOMAIN)
        cursors = await session.scalar(text("SELECT count(*) FROM mat_value_cursors"))
        callers = await session.scalar(text("SELECT callers FROM resolve_misses"))
    assert {row.max_values for row in rows.values()} == {limit}
    assert not any(row.overflowed for row in rows.values())
    assert cursors == 0
    assert callers == []


async def test_an_unmarked_branch_past_its_limit_is_never_served(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    """A schema commit lowered the limit while a writer of another branch grew it: the size
    row is past `max_values` without the mark. Reads skip it and the next pass drops it."""
    clock = Clock()
    await seeded(sessions, runtime, clock)
    await sync(sessions, runtime.settings)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE mat_value_attributes SET max_values = 2 "
                "WHERE domain_id = :d AND attribute_id = :a"
            ),
            {"d": DOMAIN, "a": COLOR},
        )
    async with sessions() as session:
        await scope_to_domain(session, DOMAIN)
        layout = await resolve_layout(session, DOMAIN, "main")
        assert not (await values.held_selection(session, DOMAIN))[(PRODUCT, COLOR)].overflowed
        assert (PRODUCT, COLOR) not in await values.selection(session, DOMAIN, layout)
        assert (PRODUCT, SIZE) in await values.selection(session, DOMAIN, layout)
        assert await values.exact_values(session, layout, ["rosso"], per_term=5) == []
        assert await values.indexed_counts(session, layout, PRODUCT, COLOR, ["Rosso"]) == {
            "Rosso": 0
        }
        for definer in (True, False):
            reads = runtime.settings.reads.model_copy(update={"use_definer_functions": definer})
            similar = await values.similar_values(
                session,
                layout,
                ["rossi"],
                threshold=0.3,
                per_term=3,
                runtime=StoreRuntime.from_settings(
                    runtime.settings.model_copy(update={"reads": reads})
                ),
            )
            assert all(h.attribute_id != COLOR for h in similar), definer
    assert (PRODUCT, COLOR) in (await sync(sessions, runtime.settings)).removed


async def test_the_fence_waits_for_writers_and_spares_other_domains(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock, count=10)
    async with sessions() as session:
        await create_domain(
            session,
            Domain(id="other", display_name="Other", description="Another Domain."),
            author=AUTHOR,
            runtime=runtime,
            clock=clock,
        )
    # A writer of the Domain holds the shared fence (uncommitted) while a pass adds Attributes.
    async with sessions() as writer:
        await writer.begin()
        await writer.execute(
            text(
                'UPDATE mat_records SET "values" = jsonb_set("values", '
                "ARRAY[CAST(:color AS text)], to_jsonb(CAST('Nuovo' AS text))) "
                "WHERE domain_id = :d AND branch = 'main' AND key = 'P-001'"
            ),
            {"d": DOMAIN, "color": COLOR},
        )
        waiting = asyncio.create_task(sync(sessions, runtime.settings))
        await wait_for_lock_waiter(engine, waiting, locktype="advisory", what="value fence")
        await writer.commit()
    await asyncio.wait_for(waiting, 30)
    assert ("main", COLOR, "Nuovo") in await agree(sessions)

    # While a pass holds the fence, writers of other Domains go on; the Domain's own wait.
    async with sessions() as holder:
        await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(lb_values_lock_key(:d))"), {"d": DOMAIN}
        )
        other = Domain(id="other", display_name="Other", description="Another Domain.")
        assert other.id == "other"
        async with sessions() as session:
            await asyncio.wait_for(
                commit(
                    session,
                    "other",
                    "main",
                    [],
                    schema_changes=[SchemaUpsert(product_kind())],
                    author=AUTHOR,
                    message="schema",
                    origin=ORIGIN,
                    runtime=runtime,
                    clock=clock,
                ),
                10,
            )
        async with sessions() as session:
            await asyncio.wait_for(
                commit(
                    session,
                    "other",
                    "main",
                    [product(n) for n in range(5)],
                    author=AUTHOR,
                    message="data",
                    origin=ORIGIN,
                    runtime=runtime,
                    clock=clock,
                ),
                10,
            )
        blocked = asyncio.create_task(run(sessions, runtime, clock, [product(2, color="Blu")]))
        await wait_for_lock_waiter(engine, blocked, locktype="advisory", what="value fence")
        await holder.rollback()
    await asyncio.wait_for(blocked, 30)
    await agree(sessions)


async def test_a_schema_change_drops_values_in_the_same_commit(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    await sync(sessions, runtime.settings)
    kind = product_kind()
    color = kind.attributes[COLOR].model_copy(update={"sensitivity": (Sensitivity.CONFIDENTIAL,)})
    changed = kind.model_copy(update={"attributes": {**kind.attributes, COLOR: color}})
    await run(sessions, runtime, clock, [], schema_changes=[SchemaUpsert(changed)])
    async with sessions() as session:
        held_pairs = await values.selection(session, DOMAIN)
        left = await session.scalar(
            text("SELECT count(*) FROM mat_values WHERE domain_id = :d AND attribute_id = :a"),
            {"d": DOMAIN, "a": COLOR},
        )
        queued = await session.scalar(
            text("SELECT count(*) FROM jobs WHERE type = 'value_index' AND domain_id = :d"),
            {"d": DOMAIN},
        )
    assert (PRODUCT, COLOR) not in held_pairs
    assert left == 0
    assert queued == 1


async def test_branches_copies_promotions_and_deletions_keep_the_index(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock, count=12)
    await sync(sessions, runtime.settings)
    for name, mode in (("copy", BranchMode.COPY), ("ov", BranchMode.OVERLAY)):
        async with sessions() as session:
            await create_branch(
                session,
                DOMAIN,
                name,
                created_by="usr_test",
                source="main",
                mode=mode,
                runtime=runtime,
            )
    await run(sessions, runtime, clock, [product(3, color="Viola")], branch="ov")
    index = await agree(sessions)
    assert ("copy", COLOR, "Rosso") in index
    assert ("ov", COLOR, "Rosso") not in index  # an overlay holds its own rows only
    async with sessions() as session, session.begin():
        assert await promote(session, DOMAIN, "ov", runtime=runtime)
    index = await agree(sessions)
    assert index[("ov", COLOR, "Viola")] == 1
    assert index[("ov", COLOR, "Rosso")] == index[("main", COLOR, "Rosso")] - 1
    async with sessions() as session, session.begin():
        await delete_branch(session, DOMAIN, "copy", runtime=runtime)
    assert not any(b == "copy" for b, _, _ in await agree(sessions))


async def test_reads_ignore_values_of_attributes_no_longer_held(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock, count=6)
    await sync(sessions, runtime.settings)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO mat_values (domain_id, branch, kind_id, attribute_id, value, norm, "
                "records) VALUES (:d, 'main', :k, :a, 'stray', 'stray', 1)"
            ),
            {"d": DOMAIN, "k": PRODUCT, "a": NOTES},
        )
    async with sessions() as session:
        layout = await resolve_layout(session, DOMAIN, "main")
        assert await values.exact_values(session, layout, ["stray"], per_term=5) == []
    report = await sync(sessions, runtime.settings, force=True)
    assert report.orphans == 1
    await agree(sessions)


def sensitive_color() -> Kind:
    kind = product_kind()
    color = kind.attributes[COLOR].model_copy(update={"sensitivity": (Sensitivity.CONFIDENTIAL,)})
    return kind.model_copy(update={"attributes": {**kind.attributes, COLOR: color}})


async def test_a_schema_change_landing_before_the_fence_is_read_again(
    sessions: Sessions, runtime: StoreRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    # A value_index job is running: the one the schema commit queues must not merge into it.
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO jobs (type, domain_id, payload, status) "
                "VALUES ('value_index', :d, '{}', 'running')"
            ),
            {"d": DOMAIN},
        )
    fence = values._fence

    async def schema_commit_first(*args: Any, **kwargs: Any) -> None:
        # The pass decided on the old schema (color held); this commit lands before the lock.
        await run(sessions, runtime, clock, [], schema_changes=[SchemaUpsert(sensitive_color())])
        await fence(*args, **kwargs)

    monkeypatch.setattr(values, "_fence", schema_commit_first)
    report = await sync(sessions, runtime.settings)
    assert (PRODUCT, COLOR) not in report.selected
    assert (PRODUCT, COLOR) not in report.added
    async with sessions() as session:
        held_pairs = await values.selection(session, DOMAIN)
        left = await session.scalar(
            text("SELECT count(*) FROM mat_values WHERE domain_id = :d AND attribute_id = :a"),
            {"d": DOMAIN, "a": COLOR},
        )
        queued = await session.scalar(
            text(
                "SELECT count(*) FROM jobs WHERE type = 'value_index' AND domain_id = :d "
                "AND status = 'queued'"
            ),
            {"d": DOMAIN},
        )
    assert (PRODUCT, COLOR) not in held_pairs
    assert left == 0
    assert queued == 1
    await agree(sessions)


async def test_a_pass_that_changes_nothing_takes_no_lock(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    await sync(sessions, runtime.settings)
    async with sessions() as writer:
        await writer.begin()
        # A writer of the Domain holds the shared fence for its whole transaction.
        await writer.execute(
            text("SELECT pg_advisory_xact_lock_shared(lb_values_lock_key(:d))"), {"d": DOMAIN}
        )
        report = await asyncio.wait_for(sync(sessions, runtime.settings, force=True), 10)
        await writer.rollback()
    assert not report.skipped
    assert report.added == []
    assert report.removed == []


async def test_the_backfill_stops_at_the_budget(sessions: Sessions, runtime: StoreRuntime) -> None:
    clock = Clock()
    await seeded(sessions, runtime, clock)
    async with sessions() as session, session.begin():
        done, left, written = await values._backfill(
            session,
            DOMAIN,
            [(PRODUCT, COLOR), (PRODUCT, SIZE)],
            64,
            time.monotonic() - 1,
            caps={(PRODUCT, COLOR): 10, (PRODUCT, SIZE): 10},
        )
    assert (done, written) == ([], 0)
    assert left == [(PRODUCT, COLOR), (PRODUCT, SIZE)]
