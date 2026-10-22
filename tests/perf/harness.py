"""Read-path performance harness: seed the `supply` Domain at scale, then time the hot paths.

The dataset comes from `examples/supply/scripts/make_supply_data.py` (deterministic) and goes
through the real commit pipeline, so materialized rows, search vectors and per-Attribute
indexes are the ones production builds. `extra_links` adds that many `supplied_by` and
`stocked_at` Links per Part to reach a denser graph (about three Links per Record).

Each measured operation is shaped like one agent request: a transaction that scopes RLS to
the Domain and runs the store primitive. Timings are client side over a local socket.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Link, Record, Schema, load_schema
from lorebase_core.revision import Author, Origin
from lorebase_core.types import AttributeType, encode_value
from lorebase_core.validate import DataChange, LinkUpsert, RecordUpsert
from lorebase_keeper.reading.points import KindScope, ReadPoint, ServedFrom
from lorebase_keeper.reading.record_history import record_history, record_lineage
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.search import partial_matching, typo_tolerance
from lorebase_keeper.reading.settings import ReadSettings, SearchSettings
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.definer import grant_read_functions
from lorebase_store.indexes import BranchIndexes, run_index_jobs
from lorebase_store.layout import BranchLayout, load_layout
from lorebase_store.materialize import (
    BranchDeletion,
    BranchMode,
    MaterializationMode,
    background,
    create_branch,
    delete_branch,
)
from lorebase_store.models.enums import RefKind
from lorebase_store.reads import (
    Comparison,
    get_record,
    head_schema,
    index_spec,
    links_of_record,
    list_records,
    query_by_attribute,
    search_corrections,
    search_records,
)
from lorebase_store.repositories import (
    DomainRepository,
    RefRepository,
    RevisionRepository,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.traverse import neighborhood

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "supply"
DOMAIN = "supply"
AUTHOR = Author(principal="usr_perf")
ORIGIN = Origin(type="perf")
READER_ROLE = "lorebase_perf_reader"

RECORD_FILES = {
    "supplier": "Supplier",
    "part": "Part",
    "plant": "Plant",
    "purchase_order": "PurchaseOrder",
    "contract": "Contract",
    "risk_flag": "RiskFlag",
}
LINK_FILES = ("supplied_by", "stocked_at", "for_part", "delivered_to", "with_supplier", "flags")
#: Record files committed together with the Links their Kind requires (`required_from`).
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("supplier", ()),
    ("plant", ()),
    ("part", ("supplied_by", "stocked_at")),
    ("purchase_order", ("for_part", "delivered_to")),
    ("contract", ("with_supplier",)),
    ("risk_flag", ("flags",)),
)

#: Keeper's lexical search with its typo tolerance defaults (`search.trigram_threshold`,
#: `search.typo_*`), on a misspelled title word (`misspelled`): corrections, then search.
TYPO_SEARCH = "typo-tolerant full-text search"
#: Keeper's lexical search on a descriptive query: three title words that rarely share a
#: Record, so the full match finds too few and the ranked partial matches follow
#: (`search.partial`), corrections included.
PARTIAL_SEARCH = "partial-match full-text search"

TARGETS_MS = {
    "get by key": 5.0,
    "list page of 50": 20.0,
    "links of a record": 10.0,
    "2-hop neighborhood": 30.0,
    "query by indexed attribute": 25.0,
    "full-text search": 50.0,
    TYPO_SEARCH: 50.0,
    PARTIAL_SEARCH: 50.0,
    "record history page": 10.0,
    "record lineage": 10.0,
}
#: Reads without a budget in design 8.7: held to the budget of Links of a Record, the other
#: per-Record index range read.
PROPOSED_TARGETS = frozenset({"record history page", "record lineage"})


def misspelled(word: str) -> str:
    """`word` with the two letters in its middle swapped (`supplier` -> `suplpier`)."""
    middle = len(word) // 2
    swapped = word[: middle - 1] + word[middle] + word[middle - 1] + word[middle + 1 :]
    return swapped if swapped != word else word + word[-1]


@dataclass(frozen=True, slots=True)
class Scale:
    factor: int
    extra_links: int
    commit_batch: int = 10000
    seed: int = 20260115
    #: Largest generator scale built at once (about 1.1M Records). A larger factor is built
    #: in shards of at most this scale with their own seeds and key ranges, so that memory
    #: stays bounded; a shard links only its own Records.
    shard_factor: int = 6000
    #: Copies of the loaded dataset on `main`: with more than one, `factor / replicas` goes
    #: through the commit pipeline and `replicate` copies its materialized rows with
    #: renumbered keys.
    replicas: int = 1

    def shards(self) -> list[int]:
        """Generator scale of each shard."""
        count = -(-self.factor // self.shard_factor)
        size, larger = divmod(self.factor, count)
        return [size + 1] * larger + [size] * (count - larger)


@dataclass
class SeedReport:
    records: int
    links: int
    seconds: float
    commits: int


@dataclass
class Measurement:
    name: str
    samples_ms: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def p95(self) -> float:
        ordered = sorted(self.samples_ms)
        return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]

    @property
    def p99(self) -> float:
        ordered = sorted(self.samples_ms)
        return ordered[min(len(ordered) - 1, round(0.99 * (len(ordered) - 1)))]


# Data -------------------------------------------------------------------------------------------


def generator() -> ModuleType:
    path = EXAMPLE / "scripts" / "make_supply_data.py"
    spec = importlib.util.spec_from_file_location("make_supply_data", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def perf_schema() -> Schema:
    """The example schema without Operations and Derivations (CEL is not enabled in commits).

    Attributes computed by a Derivation are dropped with it.
    """
    schema = load_schema((EXAMPLE / "schema.yaml").read_text(encoding="utf-8"))
    kinds = {}
    for kind in schema.kinds.values():
        kept = {a.id: a for a in kind.attributes.values() if a.derivation_id is None}
        kinds[kind.id] = kind.model_copy(update={"attributes": kept})
    return schema.model_copy(update={"kinds": kinds, "operations": {}, "derivations": {}})


def _typed(attribute: Any, raw: str) -> Any:
    if raw == "":
        return None
    if attribute.type.name is AttributeType.BOOLEAN:
        return raw == "true"
    return encode_value(attribute.type, raw)


def build_changes(schema: Schema, scale: Scale) -> Iterator[list[DataChange]]:
    """Batches of changes, each a group of Records with the Links their Kind requires.

    Built lazily, one shard and one Kind at a time, so that only one shard's generator tables
    and one Kind's changes are in memory at once.
    """
    module = generator()
    offsets = dict.fromkeys(KEY_COLUMNS, 0)
    for shard, factor in enumerate(scale.shards()):
        seed = scale.seed + shard
        tables = module.build_dataset(
            module.Settings(factor, seed, date.fromisoformat(module.DEFAULT_AS_OF))
        )
        _densify(tables, scale, random.Random(seed))
        if shard:
            _shift_keys(module, tables, offsets)
        for stem in offsets:
            offsets[stem] += len(tables[stem])
        yield from _batches(schema, scale, tables)


#: Record files with numbered keys: the key column and the generator's key kind.
KEY_COLUMNS = {
    "supplier": ("supplier_code", "supplier"),
    "part": ("part_number", "part"),
    "plant": ("plant_code", "plant"),
    "purchase_order": ("po_number", "order"),
    "contract": ("contract_number", "contract"),
}


def _shift_keys(
    module: ModuleType, tables: dict[str, list[dict[str, str]]], offsets: dict[str, int]
) -> None:
    """Renumber a shard's keys after those of the shards before it, in every column that
    holds one; VAT numbers follow the Supplier's number, as the generator derives them."""
    renamed: dict[str, str] = {}
    for stem, (column, kind) in KEY_COLUMNS.items():
        prefix, start = module.KEY_PREFIX[kind], module.KEY_START[kind]
        for row in tables[stem]:
            index = int(row[column].removeprefix(prefix)) - start + offsets[stem]
            new = module.key(kind, index)
            renamed[row[column]] = new
            row[column] = new
            if stem == "supplier":
                row["vat_number"] = f"{row['country']}{(index + 1) * 7919 % 10**9:09d}"
    for row in tables["purchase_order"]:
        row["supplier"] = renamed[row["supplier"]]
    for stem in LINK_FILES:
        for row in tables[stem]:
            row["from_key"] = renamed.get(row["from_key"], row["from_key"])
            row["to_key"] = renamed.get(row["to_key"], row["to_key"])


def _batches(
    schema: Schema, scale: Scale, tables: dict[str, list[dict[str, str]]]
) -> Iterator[list[DataChange]]:
    for stem, link_stems in GROUPS:
        kind = schema.kind_by_api_name(RECORD_FILES[stem])
        assert kind is not None
        records: list[RecordUpsert] = []
        for row in tables[stem]:
            values = {}
            for column, raw in row.items():
                if column == "key":
                    continue
                attribute = kind.attribute_by_api_name(column)
                if attribute is None:
                    continue
                value = _typed(attribute, raw)
                if value is not None:
                    values[attribute.id] = value
            key = row["key"] if "key" in row else row[kind.attributes[kind.primary_key[0]].api_name]
            records.append(RecordUpsert(record=Record(kind=kind.id, key=key, values=values)))
        links_by_from: dict[str, list[LinkUpsert]] = {}
        for link_stem in link_stems:
            found = schema.relation_by_api_name(link_stem)
            assert found is not None
            relation = found[0]
            for row in tables[link_stem]:
                values = {}
                for column, raw in row.items():
                    if column in ("from_key", "to_key"):
                        continue
                    attribute = next(
                        a for a in relation.attributes.values() if a.api_name == column
                    )
                    value = _typed(attribute, raw)
                    if value is not None:
                        values[attribute.id] = value
                link = Link.model_validate(
                    {
                        "relation": relation.id,
                        "from": row["from_key"],
                        "to": row["to_key"],
                        "values": values,
                    }
                )
                links_by_from.setdefault(link.from_key, []).append(LinkUpsert(link=link))
        batch: list[DataChange] = []
        for change in records:
            batch.append(change)
            batch.extend(links_by_from.pop(change.record.key, []))
            if len(batch) >= scale.commit_batch:
                yield batch
                batch = []
        if batch:
            yield batch
        del tables[stem]


def _densify(tables: dict[str, list[dict[str, str]]], scale: Scale, rng: random.Random) -> None:
    """`extra_links` more suppliers and plants per Part, distinct from the existing ones."""
    if scale.extra_links < 1:
        return
    suppliers = [row["supplier_code"] for row in tables["supplier"]]
    plants = [row["plant_code"] for row in tables["plant"]]
    for stem, targets, template in (
        ("supplied_by", suppliers, {"is_primary": "false", "lead_time_days": "30"}),
        ("stocked_at", plants, {"quantity": "10"}),
    ):
        existing: dict[str, set[str]] = {}
        columns = list(tables[stem][0])
        for row in tables[stem]:
            existing.setdefault(row["from_key"], set()).add(row["to_key"])
        for part in tables["part"]:
            number = part["part_number"]
            taken = existing.setdefault(number, set())
            wanted = min(scale.extra_links, len(targets) - len(taken))
            while wanted > 0:
                target = rng.choice(targets)
                if target in taken:
                    continue
                taken.add(target)
                row = {"from_key": number, "to_key": target, **template}
                tables[stem].append({c: row.get(c, "") for c in columns})
                wanted -= 1


async def seed(
    engine: AsyncEngine, runtime: StoreRuntime, scale: Scale, *, resume: bool = False
) -> SeedReport:
    """Create the Domain, its schema and the dataset on `main`; refresh statistics.

    The load sends materialized rows through COPY from `LOAD_COPY_THRESHOLD_ROWS` rows on
    (a seed commit writes a few thousand per Kind or Relation); the measurements use
    `runtime` as given. `resume` continues an interrupted load: the batches are
    deterministic, so those already committed are skipped (`seconds` then counts this run)."""
    writes = runtime.settings.writes.model_copy(
        update={"copy_threshold_rows": LOAD_COPY_THRESHOLD_ROWS}
    )
    runtime = StoreRuntime.from_settings(runtime.settings.model_copy(update={"writes": writes}))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    schema = perf_schema()
    started = time.perf_counter()
    done = await _seed_commits(engine) if resume else None
    if done is None:
        await _create_schema(sessions, schema, runtime)
        done = 0
    await _drop_deferred_indexes(engine)
    if scale.factor % scale.replicas:
        raise ValueError(f"scale {scale.factor} is not a multiple of {scale.replicas} replicas")
    loaded = replace(scale, factor=scale.factor // scale.replicas, replicas=1)
    batches = 0
    for batch in build_changes(schema, loaded):
        batches += 1
        if batches <= done:
            continue
        if batches % 10 == 0:
            _progress(f"seed: {batches} commits, {time.perf_counter() - started:.0f} s")
        async with sessions() as session:
            await commit(
                session,
                DOMAIN,
                "main",
                batch,
                author=AUTHOR,
                message="seed",
                origin=ORIGIN,
                runtime=runtime,
            )
    if scale.replicas > 1:
        await replicate(engine, schema, loaded.factor, scale.replicas)
    _progress(f"seed: {batches} commits, building indexes")
    await _create_deferred_indexes(engine)
    await build_indexes(engine, runtime)
    await _analyze(engine)
    seconds = time.perf_counter() - started
    _progress(f"seed: done in {seconds:.0f} s")
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        records = (
            await session.execute(
                text("SELECT count(*) FROM mat_records WHERE domain_id = :d AND branch = 'main'"),
                {"d": DOMAIN},
            )
        ).scalar_one()
        links = (
            await session.execute(
                text("SELECT count(*) FROM mat_links WHERE domain_id = :d AND branch = 'main'"),
                {"d": DOMAIN},
            )
        ).scalar_one()
    return SeedReport(int(records), int(links), seconds, batches + 1)


LOAD_COPY_THRESHOLD_ROWS = 500

#: Indexes no commit reads, dropped while the dataset loads and built once afterwards (a GIN
#: index is cheaper to build than to maintain row by row): full-text search and values.
DEFERRED_INDEXES = {
    "ix_mat_records_branch_search": "CREATE INDEX ix_mat_records_branch_search "
    "ON public.mat_records USING gin (domain_id, branch, search)",
    "ix_mat_records_values": "CREATE INDEX ix_mat_records_values "
    'ON public.mat_records USING gin ("values" jsonb_path_ops)',
}


#: Renumbers a key of the generator in replica `s`: numbered keys move by `s` times the
#: count of their prefix (`n`, a JSON object), ULID keys (Risk flags, which start with 0 in
#: the generator's time range) get a first character of their own; other text is kept.
#: `lb_rv` applies it to the text values of a Record and gives unique Attributes (`uniq`)
#: the replica number.
_RENUMBER = r"""
CREATE FUNCTION pg_temp.lb_rk(k text, s int, n jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN k ~ '^(S|P|PL|PO|C)-[0-9]+$' THEN
      split_part(k, '-', 1) || '-'
      || (split_part(k, '-', 2)::bigint + s * (n ->> split_part(k, '-', 1))::bigint)
    WHEN k ~ '^0[0-9A-HJKMNP-TV-Z]{25}$' THEN
      substr('123456789ABCDEFGHJKMNPQRSTVWXYZ', s, 1) || substr(k, 2)
    ELSE k
  END $$;
CREATE FUNCTION pg_temp.lb_rv(v jsonb, s int, n jsonb, uniq text[]) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(jsonb_object_agg(e.key, CASE
    WHEN jsonb_typeof(e.value) <> 'string' THEN e.value
    WHEN e.key = ANY(uniq) THEN
      to_jsonb(left(e.value #>> '{}', 2) || lpad(s::text, 2, '0') || substr(e.value #>> '{}', 3))
    ELSE to_jsonb(pg_temp.lb_rk(e.value #>> '{}', s, n))
  END), '{}'::jsonb)
  FROM jsonb_each(v) AS e $$;
"""
_REPLICA_RECORDS = """
INSERT INTO mat_records (domain_id, branch, kind_id, key, "values", derived, title, search,
    object_hash, lineage, updated_revision, updated_at, revision_generation, deleted,
    lineage_revisions)
SELECT domain_id, branch, kind_id, pg_temp.lb_rk(key, :s, CAST(:n AS jsonb)),
    pg_temp.lb_rv("values", :s, CAST(:n AS jsonb), CAST(:uniq AS text[])), derived,
    CASE WHEN title = key THEN pg_temp.lb_rk(title, :s, CAST(:n AS jsonb)) ELSE title END,
    search, object_hash, lineage, updated_revision, updated_at, revision_generation, deleted,
    lineage_revisions
FROM mat_records WHERE domain_id = :d AND branch = 'main' AND key = ANY(CAST(:keys AS text[]))
"""
_REPLICA_LINKS = """
INSERT INTO mat_links (domain_id, branch, relation_id, from_key, to_key, "values",
    object_hash, lineage, revision_generation, deleted)
SELECT domain_id, branch, relation_id, pg_temp.lb_rk(from_key, :s, CAST(:n AS jsonb)),
    pg_temp.lb_rk(to_key, :s, CAST(:n AS jsonb)), "values", object_hash, lineage,
    revision_generation, deleted
FROM mat_links WHERE domain_id = :d AND branch = 'main' AND from_key = ANY(CAST(:keys AS text[]))
"""
_REPLICA_HISTORY = """
INSERT INTO mat_history (domain_id, branch, partition, element_key, revision_id, at, old_hash,
    new_hash, provenance)
SELECT domain_id, branch, partition,
    CASE WHEN partition LIKE 'k:%' THEN pg_temp.lb_rk(element_key, :s, CAST(:n AS jsonb))
    ELSE pg_temp.lb_rk(split_part(element_key, chr(31), 1), :s, CAST(:n AS jsonb)) || chr(31)
        || pg_temp.lb_rk(split_part(element_key, chr(31), 2), :s, CAST(:n AS jsonb)) END,
    revision_id, at, old_hash, new_hash, provenance
FROM mat_history
WHERE domain_id = :d AND branch = 'main' AND (partition LIKE 'k:%' OR partition LIKE 'r:%')
  AND split_part(element_key, chr(31), 1) = ANY(CAST(:keys AS text[]))
"""
#: Generator key kinds and their `BASE_COUNTS` entries.
REPLICA_KINDS = {
    "supplier": "suppliers",
    "part": "parts",
    "plant": "plants",
    "order": "orders",
    "contract": "contracts",
}
#: Keys per replication statement, and replicas written at once.
REPLICA_BATCH_KEYS = 20000
REPLICA_PARALLEL = 4


async def replicate(engine: AsyncEngine, schema: Schema, factor: int, replicas: int) -> None:
    """Grow `main` to `replicas` copies of the Records, Links and history loaded at generator
    scale `factor`, with renumbered keys, by set-based SQL: at 10M Records the commit
    pipeline needs about ten hours on a laptop, mostly random index reads and writes of
    content-addressed objects and tree nodes.

    Only the materialized head is copied, which every read reads: Revisions, trees and
    objects stay those of the loaded copy, and the copies' rows point to its Revisions and
    objects. Search vectors are copied (titles equal to the key are renumbered); the word
    lexicon of `main` is counted again from the search vectors afterwards (its triggers,
    which serialize parallel writers on shared words, are off meanwhile); per-Attribute
    indexes are dropped and built again afterwards. A replica already present (its first
    Supplier exists) is skipped, so an interrupted run resumes."""
    unique = sorted(a.id for k in schema.kinds.values() for a in k.attributes.values() if a.unique)
    module = generator()
    prefixes = {kind: module.KEY_PREFIX[kind].rstrip("-") for kind in REPLICA_KINDS}
    counts = {
        prefixes[kind]: module.BASE_COUNTS[stem] * factor for kind, stem in REPLICA_KINDS.items()
    }
    ends = {prefixes[kind]: module.KEY_START[kind] + counts[prefixes[kind]] for kind in prefixes}
    async with engine.connect() as connection:
        # The loaded copy: numbered keys before their first replica, ULIDs as generated.
        keys = list(
            (
                await connection.execute(
                    text(
                        "SELECT key FROM mat_records WHERE domain_id = :d AND branch = 'main' "
                        "AND (key LIKE '0%' OR (key ~ '^[A-Z]+-[0-9]+$' AND "
                        "split_part(key, '-', 2)::bigint < "
                        "(CAST(:ends AS jsonb) ->> split_part(key, '-', 1))::bigint)) "
                        "ORDER BY key"
                    ),
                    {"d": DOMAIN, "ends": json.dumps(ends)},
                )
            ).scalars()
        )
    numbers = json.dumps(counts)
    attribute_indexes = await _drop_attribute_indexes(engine)
    gate = asyncio.Semaphore(REPLICA_PARALLEL)

    async def one(replica: int) -> None:
        async with gate, engine.begin() as connection:
            supplier = prefixes["supplier"]
            number = module.KEY_START["supplier"] + replica * counts[supplier]
            moved = f"{supplier}-{number}"
            present = await connection.execute(
                text(
                    "SELECT 1 FROM mat_records WHERE domain_id = :d AND branch = 'main' "
                    "AND key = :k"
                ),
                {"d": DOMAIN, "k": moved},
            )
            if present.first() is not None:
                _progress(f"replica {replica}: present")
                return
            for statement in filter(str.strip, _RENUMBER.split(";\n")):
                await connection.exec_driver_sql(statement)
            for start in range(0, len(keys), REPLICA_BATCH_KEYS):
                chunk = keys[start : start + REPLICA_BATCH_KEYS]
                params = {"d": DOMAIN, "s": replica, "n": numbers, "keys": chunk}
                await connection.execute(text(_REPLICA_RECORDS), {**params, "uniq": unique})
                await connection.execute(text(_REPLICA_LINKS), params)
                await connection.execute(text(_REPLICA_HISTORY), params)
            _progress(f"replica {replica}: written")

    _progress(f"replicating {len(keys):,} Records {replicas - 1} times")
    await _lexicon_triggers(engine, enabled=False)
    try:
        await asyncio.gather(*(one(replica) for replica in range(1, replicas)))
    finally:
        await _lexicon_triggers(engine, enabled=True)
    await _recount_lexicon(engine)
    await _create_attribute_indexes(engine, attribute_indexes)


LEXICON_TRIGGERS = ("lb_lexicon_insert", "lb_lexicon_update", "lb_lexicon_delete")


async def _lexicon_triggers(engine: AsyncEngine, *, enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    async with engine.begin() as connection:
        for name in LEXICON_TRIGGERS:
            await connection.exec_driver_sql(f"ALTER TABLE mat_records {action} TRIGGER {name}")


async def _recount_lexicon(engine: AsyncEngine) -> None:
    """The word lexicon of `main` counted from its search vectors, as migration `0018`
    fills it."""
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM mat_lexicon WHERE domain_id = :d AND branch = 'main'"),
            {"d": DOMAIN},
        )
        await connection.execute(
            text(
                "INSERT INTO mat_lexicon (domain_id, branch, kind_id, word, records) "
                "SELECT m.domain_id, m.branch, m.kind_id, w.lexeme, count(*) "
                "FROM mat_records m CROSS JOIN LATERAL unnest(m.search) AS w "
                "WHERE m.domain_id = :d AND m.branch = 'main' AND m.search IS NOT NULL "
                "AND NOT m.deleted AND lb_lexicon_word(w.lexeme) "
                "GROUP BY m.domain_id, m.branch, m.kind_id, w.lexeme"
            ),
            {"d": DOMAIN},
        )


async def _drop_attribute_indexes(engine: AsyncEngine) -> list[str]:
    """Drop the per-Attribute indexes of `main`; returns the statements that build them."""
    async with engine.begin() as connection:
        found = (
            await connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                    "AND indexname LIKE 'mat_records_attr_%' AND indexdef LIKE :branch"
                ),
                {"branch": "%branch = 'main'::text%"},
            )
        ).all()
        for name, _ in found:
            await connection.exec_driver_sql(f"DROP INDEX {name}")
    return [definition for _, definition in found]


async def _create_attribute_indexes(engine: AsyncEngine, statements: Sequence[str]) -> None:
    gate = asyncio.Semaphore(REPLICA_PARALLEL)

    async def one(statement: str) -> None:
        async with gate, engine.begin() as connection:
            await connection.exec_driver_sql("SET LOCAL maintenance_work_mem = '512MB'")
            await connection.exec_driver_sql(statement)

    await asyncio.gather(*(one(statement) for statement in statements))


async def _create_schema(
    sessions: async_sessionmaker[AsyncSession], schema: Schema, runtime: StoreRuntime
) -> None:
    async with sessions() as session:
        await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime)
    elements = [*schema.kinds.values(), *schema.relations.values(), *schema.traits.values()]
    elements.extend(schema.sources.values())
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(e) for e in elements],
            author=AUTHOR,
            message="schema",
            origin=ORIGIN,
            runtime=runtime,
        )


async def _seed_commits(engine: AsyncEngine) -> int | None:
    """Seed commits already on `main`, `None` when the Domain does not exist yet."""
    async with engine.connect() as connection:
        exists = (
            await connection.execute(text("SELECT 1 FROM domains WHERE id = :d"), {"d": DOMAIN})
        ).first()
        if exists is None:
            return None
        found = await connection.execute(
            text("SELECT count(*) FROM revisions WHERE domain_id = :d AND message = 'seed'"),
            {"d": DOMAIN},
        )
        return int(found.scalar_one())


async def _drop_deferred_indexes(engine: AsyncEngine) -> None:
    """Drop `DEFERRED_INDEXES`, checking that the migrations still define them as here."""
    async with engine.begin() as connection:
        found = (
            await connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                    "AND indexname = ANY(CAST(:names AS text[]))"
                ),
                {"names": list(DEFERRED_INDEXES)},
            )
        ).all()
        for name, definition in found:
            # Defined on the partitioned table: pg_indexes shows ON ONLY, while CREATE INDEX
            # without it builds every partition.
            assert definition.replace(" ON ONLY ", " ON ") == DEFERRED_INDEXES[name], definition
            await connection.exec_driver_sql(f"DROP INDEX {name}")


async def _create_deferred_indexes(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.exec_driver_sql("SET LOCAL maintenance_work_mem = '1GB'")
        for statement in DEFERRED_INDEXES.values():
            await connection.exec_driver_sql(
                statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
            )


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def build_indexes(engine: AsyncEngine, runtime: StoreRuntime) -> None:
    """Run the queued index jobs until none is left, as the job worker would."""
    while await run_index_jobs(engine, runtime.settings.indexes):
        pass


async def _analyze(engine: AsyncEngine) -> None:
    """Without parallel index vacuuming, whose shared memory exceeds a container's default
    `/dev/shm` once the indexes of several branches are large."""
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(
            "VACUUM (PARALLEL 0, ANALYZE) mat_records, mat_links, mat_lexicon, refs, mat_schema"
        )


async def create_reader(engine: AsyncEngine) -> None:
    """A role subject to row-level security, as the application role is in production, with
    EXECUTE on the security definer read functions."""
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        exists = (
            await auto.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": READER_ROLE}
            )
        ).first()
        if exists is None:
            await auto.exec_driver_sql(f"CREATE ROLE {READER_ROLE} NOLOGIN")
        await auto.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER_ROLE}")
        await grant_read_functions(auto, READER_ROLE)


async def drop_reader(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"DROP OWNED BY {READER_ROLE}")
        try:
            await auto.exec_driver_sql(f"DROP ROLE IF EXISTS {READER_ROLE}")
        except DBAPIError:
            # Roles belong to the server: another database of it (a kept seed database)
            # still grants to this one.
            _progress(f"kept role {READER_ROLE}, still used by another database")


# Measurements -----------------------------------------------------------------------------------

Operation = Callable[[AsyncSession, random.Random], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Workload:
    """Keys and values sampled from the seeded data."""

    schema: Schema
    parts: list[str]
    suppliers: list[str]
    words: list[str]
    supplier_kind: str
    part_kind: str


async def workload(engine: AsyncEngine, runtime: StoreRuntime) -> Workload:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, "main", runtime=runtime)
        assert found is not None
        schema = found.schema
        part = schema.kind_by_api_name("Part")
        supplier = schema.kind_by_api_name("Supplier")
        assert part is not None
        assert supplier is not None
        keys = text(
            "SELECT key FROM mat_records WHERE domain_id = :d AND branch = 'main' AND kind_id = :k"
        )
        parts = list((await session.execute(keys, {"d": DOMAIN, "k": part.id})).scalars())
        suppliers = list((await session.execute(keys, {"d": DOMAIN, "k": supplier.id})).scalars())
        titles = text(
            "SELECT title FROM mat_records WHERE domain_id = :d AND branch = 'main' "
            "AND title IS NOT NULL ORDER BY key LIMIT 2000"
        )
        words = sorted(
            {
                word.lower()
                for title in (await session.execute(titles, {"d": DOMAIN})).scalars()
                for word in title.split()
                if word.isalpha() and len(word) > 3
            }
        )
    return Workload(schema, parts, suppliers, words, supplier.id, part.id)


def read_operations(
    work: Workload, runtime: StoreRuntime, branch: str | BranchLayout = "main"
) -> dict[str, Operation]:
    """The timed reads on `branch`; a layout, as Keeper passes it, spares the ref lookup."""
    schema = work.schema
    supplier = schema.kinds[work.supplier_kind]
    risk = next(a for a in supplier.attributes.values() if a.api_name == "risk_score")
    risk_index = index_spec(schema, work.supplier_kind, risk.id)
    assert risk_index is not None

    async def get(session: AsyncSession, rng: random.Random) -> Any:
        return await get_record(session, DOMAIN, branch, work.part_kind, rng.choice(work.parts))

    async def page(session: AsyncSession, rng: random.Random) -> Any:
        return await list_records(
            session,
            DOMAIN,
            branch,
            work.part_kind,
            limit=50,
            after=rng.choice(work.parts),
            runtime=runtime,
        )

    async def links(session: AsyncSession, rng: random.Random) -> Any:
        return await links_of_record(
            session,
            DOMAIN,
            branch,
            schema,
            work.supplier_kind,
            rng.choice(work.suppliers),
            limit=50,
            runtime=runtime,
        )

    async def hood(session: AsyncSession, rng: random.Random) -> Any:
        return await neighborhood(
            session,
            DOMAIN,
            branch,
            schema,
            [(work.part_kind, rng.choice(work.parts))],
            depth=2,
            runtime=runtime,
        )

    async def by_attribute(session: AsyncSession, rng: random.Random) -> Any:
        return await query_by_attribute(
            session,
            DOMAIN,
            branch,
            risk_index,
            Comparison.EQ,
            str(rng.randrange(0, 100)),
            limit=50,
            runtime=runtime,
        )

    async def search(session: AsyncSession, rng: random.Random) -> Any:
        return await search_records(
            session, DOMAIN, branch, rng.choice(work.words), limit=20, runtime=runtime
        )

    reading = ReadRuntime(
        sessions=async_sessionmaker(),
        read=ReadSettings(),
        search=SearchSettings(),
        store=runtime,
    )
    part = schema.kinds[work.part_kind]
    name = branch.branch if isinstance(branch, BranchLayout) else branch

    async def point(session: AsyncSession) -> ReadPoint:
        """The read point Keeper resolves for a branch head (`reading.points`), without the
        authorization of a caller: the Domain, the ref and its head Revision."""
        domain = await DomainRepository(session).get(DOMAIN)
        ref = await RefRepository(session, DOMAIN).get(name)
        assert domain is not None
        assert ref is not None
        revision = await RevisionRepository(session, DOMAIN).require(ref.head)
        scope = KindScope(cast(ReadGuard, None))
        return ReadPoint(
            domain, ref.name, revision, schema, ServedFrom.MATERIALIZED, scope, ref.layout
        )

    async def history(session: AsyncSession, rng: random.Random) -> Any:
        key = rng.choice(work.parts)
        return await record_history(
            session,
            reading,
            await point(session),
            part,
            key,
            limit=reading.read.history.default_limit,
            cursor=None,
            values=False,
            shape=f"perf-{key}",
        )

    async def lineage(session: AsyncSession, rng: random.Random) -> Any:
        return await record_lineage(
            session,
            reading,
            await point(session),
            part,
            rng.choice(work.parts),
        )

    typos = typo_tolerance(reading)

    async def search_typos(session: AsyncSession, rng: random.Random) -> Any:
        # Keeper's lexical search: the corrections, then the search with them.
        query = misspelled(rng.choice(work.words))
        fixes = await search_corrections(session, DOMAIN, branch, query, typos, runtime=runtime)
        return await search_records(
            session,
            DOMAIN,
            branch,
            query,
            limit=20,
            corrections=fixes,
            correction_weight=typos.weight,
            runtime=runtime,
        )

    partial = partial_matching(reading)

    async def search_partial(session: AsyncSession, rng: random.Random) -> Any:
        query = " ".join(rng.sample(work.words, 3))
        fixes = await search_corrections(session, DOMAIN, branch, query, typos, runtime=runtime)
        return await search_records(
            session,
            DOMAIN,
            branch,
            query,
            limit=20,
            corrections=fixes,
            correction_weight=typos.weight,
            partial=partial,
            runtime=runtime,
        )

    return {
        "get by key": get,
        "list page of 50": page,
        "links of a record": links,
        "2-hop neighborhood": hood,
        "query by indexed attribute": by_attribute,
        "full-text search": search,
        TYPO_SEARCH: search_typos,
        PARTIAL_SEARCH: search_partial,
        "record history page": history,
        "record lineage": lineage,
    }


async def measure_reads(
    engine: AsyncEngine,
    operations: dict[str, Operation],
    *,
    iterations: int,
    warmup: int,
    role: str | None,
    seed: int = 7,
) -> dict[str, Measurement]:
    """p50/p95 of each operation; one transaction per call, like one API request."""
    rng = random.Random(seed)
    results: dict[str, Measurement] = {}
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        for name, operation in operations.items():
            measurement = Measurement(name)
            for index in range(warmup + iterations):
                started = time.perf_counter()
                async with session.begin():
                    if role is not None:
                        await session.execute(text(f"SET LOCAL ROLE {role}"))
                    await scope_to_domain(session, DOMAIN)
                    await operation(session, rng)
                if index >= warmup:
                    measurement.samples_ms.append((time.perf_counter() - started) * 1000)
            results[name] = measurement
            _progress(_summary(measurement, role))
    return results


async def measure_alternating(
    engine: AsyncEngine,
    name: str,
    operations: Sequence[Operation],
    *,
    iterations: int,
    warmup: int,
    role: str | None,
    seed: int = 7,
) -> list[Measurement]:
    """p50/p95 of the same read on several branches, called in turn with the same random
    keys (one transaction per call), so that every branch sees the same machine load."""
    rngs = [random.Random(seed) for _ in operations]
    measurements = [Measurement(name) for _ in operations]
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        for index in range(warmup + iterations):
            for operation, rng, measurement in zip(operations, rngs, measurements, strict=True):
                started = time.perf_counter()
                async with session.begin():
                    if role is not None:
                        await session.execute(text(f"SET LOCAL ROLE {role}"))
                    await scope_to_domain(session, DOMAIN)
                    await operation(session, rng)
                if index >= warmup:
                    measurement.samples_ms.append((time.perf_counter() - started) * 1000)
    for measurement in measurements:
        _progress(_summary(measurement, role))
    return measurements


def _summary(measurement: Measurement, role: str | None) -> str:
    m = measurement
    return f"{m.name} [{role or 'owner'}]: p50 {m.p50:.2f} p95 {m.p95:.2f} p99 {m.p99:.2f} ms"


@dataclass(frozen=True, slots=True)
class BranchJobs:
    """A copy of `main` and its deletion: the request transactions, and the background job
    that copies or deletes the rows (total time and each batch transaction)."""

    create: Measurement
    copy: Measurement
    copy_batches: Measurement
    delete: Measurement
    delete_job: Measurement
    delete_batches: Measurement

    def all(self) -> list[Measurement]:
        return [
            self.create,
            self.copy,
            self.copy_batches,
            self.delete,
            self.delete_job,
            self.delete_batches,
        ]


async def finish_copy(
    engine: AsyncEngine, runtime: StoreRuntime, name: str, batches: Measurement | None = None
) -> float:
    """Run the background copy of `name` to the end, batch by batch as its job does; returns
    the total milliseconds."""
    return await _run_steps(engine, runtime, name, "materialize", batches)


async def _run_steps(
    engine: AsyncEngine,
    runtime: StoreRuntime,
    name: str,
    job: str,
    batches: Measurement | None,
) -> float:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = runtime.settings.materialize
    run = background._Run(job, name, background._Pace.of(settings))
    total = time.perf_counter()
    while True:
        started = time.perf_counter()
        async with sessions() as session, session.begin():
            if job == "materialize":
                step = await background._materialize_step(
                    session, DOMAIN, name, run, runtime, lambda: datetime.now(UTC)
                )
            else:
                step = await background._delete_step(
                    session, DOMAIN, name, run, settings, lambda: datetime.now(UTC)
                )
        elapsed = time.perf_counter() - started
        if batches is not None:
            batches.samples_ms.append(elapsed * 1000)
        if step.rows:
            # Sized like the job does (`background._batches`).
            run.pace.next(max(elapsed - step.waited_s, 0.0), contended=step.contended)
        if step.finished is not None:
            assert step.finished["status"] == "done", step.finished
            return (time.perf_counter() - total) * 1000


async def measure_branch_copy(
    engine: AsyncEngine, runtime: StoreRuntime, *, iterations: int
) -> BranchJobs:
    """Create a copy of `main`, run its background copy, then delete it and run the
    background deletion. `engine` keeps the default `database.statement_timeout_ms`: every
    transaction must finish within it. Both always go to the background here, whatever
    `async_copy_min_rows` and `async_delete_min_rows` say."""
    materialize = runtime.settings.materialize.model_copy(
        update={"async_copy_min_rows": 0, "async_delete_min_rows": 0}
    )
    runtime = StoreRuntime.from_settings(
        runtime.settings.model_copy(update={"materialize": materialize})
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    jobs = BranchJobs(
        Measurement("branch creation (copy), request"),
        Measurement("branch copy, background job"),
        Measurement("branch copy, one batch"),
        Measurement("branch deletion, request"),
        Measurement("branch deletion, background job"),
        Measurement("branch deletion, one batch"),
    )
    for index in range(iterations):
        name = f"perf-{index}"
        started = time.perf_counter()
        async with sessions() as session:
            created = await create_branch(
                session, DOMAIN, name, created_by="usr_perf", mode=BranchMode.COPY, runtime=runtime
            )
        jobs.create.samples_ms.append((time.perf_counter() - started) * 1000)
        assert created.pending, "the source is above store.materialize.async_copy_min_rows"
        jobs.copy.samples_ms.append(
            await finish_copy(engine, runtime, name, batches=jobs.copy_batches)
        )
        started = time.perf_counter()
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            deletion = await delete_branch(session, DOMAIN, name, runtime=runtime)
        jobs.delete.samples_ms.append((time.perf_counter() - started) * 1000)
        assert deletion is BranchDeletion.SCHEDULED
        jobs.delete_job.samples_ms.append(
            await _run_steps(engine, runtime, name, "delete", jobs.delete_batches)
        )
        for m in jobs.all():
            _progress(f"{m.name}: p50 {m.p50:.0f} ms, max {max(m.samples_ms):.0f} ms")
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await BranchIndexes(session, DOMAIN, name, settings=runtime.settings.indexes).drop_all()
        await build_indexes(engine, runtime)
    return jobs


OVERLAY_BRANCH = "agent/perf"
COPY_BRANCH = "perf-copy"
#: Prefixes of the branches the measurements create.
PERF_BRANCH_PREFIXES = ("perf-", "agent/perf")


async def remove_perf_branches(engine: AsyncEngine, runtime: StoreRuntime) -> list[str]:
    """Delete the branches the measurements create (left by an earlier run on the same
    database), rows and per-Attribute indexes included; returns their names."""
    materialize = runtime.settings.materialize.model_copy(update={"async_delete_min_rows": 0})
    runtime = StoreRuntime.from_settings(
        runtime.settings.model_copy(update={"materialize": materialize})
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        refs = await RefRepository(session, DOMAIN).all(RefKind.BRANCH)
    names = [r.name for r in refs if r.name.startswith(PERF_BRANCH_PREFIXES)]
    for name in names:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            deletion = await delete_branch(session, DOMAIN, name, runtime=runtime)
        if deletion is BranchDeletion.SCHEDULED:
            await _run_steps(engine, runtime, name, "delete", None)
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await BranchIndexes(session, DOMAIN, name, settings=runtime.settings.indexes).drop_all()
        _progress(f"removed branch {name}")
    return names


async def measure_branch_overlay(
    engine: AsyncEngine, runtime: StoreRuntime, *, iterations: int
) -> Measurement:
    """Create an overlay branch of `main`, then delete it."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    measurement = Measurement("branch creation (overlay)")
    for index in range(iterations):
        name = f"agent/perf-{index}"
        started = time.perf_counter()
        async with sessions() as session:
            created = await create_branch(
                session,
                DOMAIN,
                name,
                created_by="usr_perf",
                mode=BranchMode.OVERLAY,
                runtime=runtime,
            )
        measurement.samples_ms.append((time.perf_counter() - started) * 1000)
        assert created.mode is MaterializationMode.OVERLAY
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await RefRepository(session, DOMAIN).delete(name)
    return measurement


@dataclass(frozen=True, slots=True)
class OverlayPair:
    """An overlay and a full copy of `main` with the same commits, `main` moved since."""

    overlay: BranchLayout
    copy: BranchLayout
    overlay_rows: int
    copy_rows: int


async def overlay_pair(
    engine: AsyncEngine, runtime: StoreRuntime, work: Workload, *, changes: int
) -> OverlayPair:
    """Fork an overlay and a copy from `main`; commit `changes` Part updates to both, then
    `changes` other Part updates to `main` (copied into the overlay before they land)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    for name, mode in ((OVERLAY_BRANCH, BranchMode.OVERLAY), (COPY_BRANCH, BranchMode.COPY)):
        async with sessions() as session:
            created = await create_branch(
                session, DOMAIN, name, created_by="usr_perf", mode=mode, runtime=runtime
            )
        if created.pending:
            await finish_copy(engine, runtime, name)
    part = work.schema.kinds[work.part_kind]
    cost = next(a for a in part.attributes.values() if a.api_name == "unit_cost")
    rng = random.Random(17)
    keys = rng.sample(work.parts, 2 * changes)
    plan = ((keys[:changes], (OVERLAY_BRANCH, COPY_BRANCH)), (keys[changes:], ("main",)))
    for round_keys, branches in plan:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            rows = await _records(session, work.part_kind, round_keys)
        updates = [
            RecordUpsert(
                record=Record(
                    kind=work.part_kind,
                    key=key,
                    values={**rows[key], cost.id: f"7.{rng.randrange(10, 99)}"},
                )
            )
            for key in round_keys
        ]
        for branch in branches:
            async with sessions() as session:
                await commit(
                    session,
                    DOMAIN,
                    branch,
                    updates,
                    author=AUTHOR,
                    message="perf overlay",
                    origin=ORIGIN,
                    runtime=runtime,
                )
    await build_indexes(engine, runtime)
    await _analyze(engine)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        overlay = await load_layout(session, DOMAIN, OVERLAY_BRANCH)
        copy = await load_layout(session, DOMAIN, COPY_BRANCH)
        own = text(
            "SELECT (SELECT count(*) FROM mat_records WHERE domain_id = :d AND branch = :b) "
            "+ (SELECT count(*) FROM mat_links WHERE domain_id = :d AND branch = :b)"
        )
        overlay_rows = (await session.execute(own, {"d": DOMAIN, "b": OVERLAY_BRANCH})).scalar()
        copy_rows = (await session.execute(own, {"d": DOMAIN, "b": COPY_BRANCH})).scalar()
    assert overlay.overlay, "the overlay must not have been promoted"
    return OverlayPair(overlay, copy, int(overlay_rows or 0), int(copy_rows or 0))


async def measure_commits(
    engine: AsyncEngine, runtime: StoreRuntime, work: Workload, *, changes: int, iterations: int
) -> Measurement:
    """Commit `changes` Part updates (one Attribute each) as one Revision."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    part = work.schema.kinds[work.part_kind]
    cost = next(a for a in part.attributes.values() if a.api_name == "unit_cost")
    measurement = Measurement(f"commit of {changes} changes")
    rng = random.Random(11)
    for index in range(iterations):
        keys = rng.sample(work.parts, changes)
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            rows = await _records(session, work.part_kind, keys)
        updates = [
            RecordUpsert(
                record=Record(
                    kind=work.part_kind,
                    key=key,
                    values={**rows[key], cost.id: f"{index + 1}.{rng.randrange(10, 99)}"},
                )
            )
            for key in keys
        ]
        started = time.perf_counter()
        async with sessions() as session:
            await commit(
                session,
                DOMAIN,
                "main",
                updates,
                author=AUTHOR,
                message="perf update",
                origin=ORIGIN,
                runtime=runtime,
            )
        measurement.samples_ms.append((time.perf_counter() - started) * 1000)
    return measurement


async def _records(session: AsyncSession, kind_id: str, keys: Sequence[str]) -> dict[str, Any]:
    result = await session.execute(
        text(
            'SELECT key, "values"::text FROM mat_records WHERE domain_id = :d AND branch = '
            "'main' AND kind_id = :k AND key = ANY(CAST(:keys AS text[]))"
        ),
        {"d": DOMAIN, "k": kind_id, "keys": list(keys)},
    )
    return {key: json.loads(values) for key, values in result}


# Report -----------------------------------------------------------------------------------------


def hardware() -> dict[str, str]:
    notes = {
        "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
    }
    if platform.system() == "Darwin":
        for key, name in (
            ("machdep.cpu.brand_string", "cpu"),
            ("hw.ncpu", "cores"),
            ("hw.memsize", "memory"),
        ):
            try:
                value = subprocess.run(
                    ["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True, check=True
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError):
                continue
            notes[name] = f"{int(value) // 2**30} GiB" if name == "memory" else value
    return notes


async def server_notes(engine: AsyncEngine) -> dict[str, str]:
    notes: dict[str, str] = {}
    async with engine.connect() as connection:
        for name, setting in (
            ("postgres", "server_version"),
            ("shared_buffers", "shared_buffers"),
            ("effective_cache_size", "effective_cache_size"),
            ("work_mem", "work_mem"),
        ):
            notes[name] = str((await connection.execute(text(f"SHOW {setting}"))).scalar_one())
    return notes
