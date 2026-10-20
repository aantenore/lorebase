"""Merge demo: the supply Domain on two branches, merged in memory.

Runs on the core engine only (no Postgres, no network):

    uv run python examples/merge_demo.py

The story. `main` holds the supply schema and its data. Risk analysts work on `model/risk`: they
add a required Attribute, rescore suppliers and retire a part. The ERP feed lands on
`source/erp`: new suppliers, a rescoring of its own and a price change. Merging them produces
the Conflicts a team meets in practice: an Attribute changed differently on both sides, a
schema change meeting new values, a delete against a modify and a Record added twice. The demo
lists them, resolves them, merges again and diffs the result against the first import.

Output is deterministic: new ids come from a counter and Revision times from a fixed clock.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import JsonValue

from lorebase_core.canonical import ObjectType
from lorebase_core.merge import (
    Conflict,
    ConflictKind,
    InMemoryObjectStore,
    MergeAttempt,
    MergedTree,
    MergeInput,
    Resolution,
    ResolutionChoice,
    apply_resolutions,
    merge,
    put_schema,
)
from lorebase_core.merge import load_schema as read_tree_schema
from lorebase_core.merge.objects import partition_element
from lorebase_core.model import (
    Attribute,
    Link,
    Record,
    Schema,
    dump_schema,
    load_schema,
    record_key,
    schema_from_document,
    validate_schema,
)
from lorebase_core.model.yaml_schema import id_names
from lorebase_core.revision import (
    Author,
    ChangesetEntry,
    InMemoryRevisionGraph,
    Origin,
    Revision,
    changeset_between,
    changeset_stats,
)
from lorebase_core.tree import (
    LINK_KEY_SEPARATOR,
    InMemoryNodeStore,
    TreeChange,
    kind_partition,
    link_key,
    relation_partition,
    tree_apply,
    tree_entries,
    tree_get,
)
from lorebase_core.types import TypeSpec, cast
from lorebase_core.validate import SchemaRules, validate_record

SUPPLY = Path(__file__).resolve().parent / "supply"
RULES = SchemaRules(allow_expressions=True)  # the supply schema has CEL Operations
CLOCK = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
Item = Record | Link


class Repo:
    """Node store, object store and Revision graph in memory (Postgres tables in the product)."""

    def __init__(self) -> None:
        self.store = InMemoryNodeStore()  # tree nodes by hash
        self.objects = InMemoryObjectStore()  # Records, Links and schema elements by hash
        self.graph = InMemoryRevisionGraph()  # parents and generations
        self.roots: dict[bytes, bytes] = {}  # Revision id -> tree root

    def commit(self, root: bytes, parents: Sequence[bytes], message: str, author: str) -> bytes:
        """Write a Revision of tree `root`; its stats come from the diff to the first parent."""
        parent_root = self.roots[parents[0]] if parents else None
        revision = Revision(
            parents=tuple(parents),
            root=root,
            domain="supply",
            author=Author(principal=author),
            message=message,
            origin=Origin(type="demo"),
            created_at=CLOCK + timedelta(minutes=len(self.roots)),
            stats=changeset_stats(changeset_between(self.store, parent_root, root)),
        )
        rid = self.graph.add_revision(revision)
        self.roots[rid] = root
        return rid

    def edit(
        self,
        parent: bytes | None,
        message: str,
        author: str,
        schema: Schema | None = None,
        put: Sequence[Item] = (),
        delete: Sequence[tuple[str, str]] = (),
    ) -> bytes:
        """Commit on top of `parent`: new schema, upserted Records and Links, deleted locations."""
        changes = [] if schema is None else put_schema(self.objects, schema)
        for item in put:
            kind = ObjectType.RECORD if isinstance(item, Record) else ObjectType.LINK
            changes.append(TreeChange(*where(item), self.objects.put(kind, item.content())))
        changes += [TreeChange(partition, key, None) for partition, key in delete]
        root = tree_apply(self.store, self.roots[parent] if parent else None, changes)
        return self.commit(root, [parent] if parent else [], message, author)

    def record(self, root: bytes, kind_id: str, key: str) -> Record:
        digest = tree_get(self.store, root, kind_partition(kind_id), key)
        return Record.model_validate(self.objects.get(digest or b""))

    def merge(self, ours: bytes, theirs: bytes) -> MergedTree | MergeAttempt:
        return merge(
            MergeInput(
                store=self.store,
                objects=self.objects,
                graph=self.graph,
                revision_root=self.roots.__getitem__,
                ours=ours,
                theirs=theirs,
            )
        )


def where(item: Item) -> tuple[str, str]:
    """Location of an item in the tree: partition and key."""
    if isinstance(item, Record):
        return kind_partition(item.kind), item.key
    return relation_partition(item.relation), link_key(item.from_key, item.to_key)


def build(schema: Schema, kind_name: str, base: Record | None = None, **by_name: str) -> Record:
    """A Record from Attribute api names and text values, or `base` with some values replaced."""
    kind = schema.kind_by_api_name(kind_name)
    assert kind is not None
    values = {} if base is None else dict(base.values)
    for name, text in by_name.items():
        attribute = kind.attribute_by_api_name(name)
        assert attribute is not None, f"{kind_name} has no Attribute {name}"
        values[attribute.id] = to_value(attribute, text)
    return Record(kind=kind.id, key=record_key(kind, values) or "", values=values)


def to_value(attribute: Attribute, text: str) -> JsonValue:
    """Text to the canonical value of the Attribute type (integers become strings, ...)."""
    return cast(TypeSpec.of("string"), attribute.type, text)


def read_csv(name: str) -> Iterator[dict[str, str]]:
    with (SUPPLY / "data" / f"{name}.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            yield {column: text for column, text in row.items() if text != ""}


def load_links(schema: Schema, relation_name: str) -> list[Link]:
    found = schema.relation_by_api_name(relation_name)
    assert found is not None
    relation = found[0]
    by_name = {a.api_name: a for a in relation.attributes.values()}  # link Attributes
    links = []
    for row in read_csv(relation_name):
        values = {a.id: to_value(a, row[n]) for n, a in by_name.items() if n in row}
        links.append(
            Link(
                relation=relation.id, from_key=row["from_key"], to_key=row["to_key"], values=values
            )
        )
    return links


# Printing ---------------------------------------------------------------------------------


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    lines = [headers, ["-" * len(h) for h in headers], *rows]
    widths = [max(len(str(line[i])) for line in lines) for i in range(len(headers))]
    for line in lines:
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(line, widths, strict=True)).rstrip())


def owner(names: dict[str, str], partition: str) -> str:
    """Api name of the Kind or Relation that owns a partition."""
    element = partition_element(partition)
    return "schema" if element is None else names[element]


def show(value: object) -> str:
    return "(absent)" if value is None else "{record}" if isinstance(value, dict) else str(value)


def describe(names: dict[str, str], c: Conflict) -> str:
    if c.grouped:  # one Conflict for many Records
        return f"{owner(names, c.partition)} x{c.count}: {', '.join(c.sample)}"
    return f"{owner(names, c.partition)} {c.key}"


def print_conflicts(conflicts: Iterable[Conflict], names: dict[str, str]) -> None:
    groups: dict[ConflictKind, list[Conflict]] = defaultdict(list)
    for conflict in conflicts:
        groups[conflict.kind].append(conflict)
    for kind in sorted(groups, key=lambda k: k.value):
        print(f"\n{kind.value} {kind.name} ({len(groups[kind])})")
        rows = []
        for c in groups[kind]:
            values = ("", "", "") if c.grouped else (show(c.base), show(c.ours), show(c.theirs))
            attribute = names.get(c.attribute or "", "-")
            rows.append(
                (describe(names, c), attribute, *values, "/".join(x.value for x in c.choices))
            )
        table(("where", "attribute", "base", "ours", "theirs", "choices"), rows)


def print_counts(changes: Iterable[ChangesetEntry], names: dict[str, str]) -> None:
    """Created, updated and deleted elements per Kind, Relation and schema."""
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for e in changes:
        action = "created" if not e.old_hash else "deleted" if not e.new_hash else "updated"
        counts[owner(names, e.partition)][action] += 1
    rows = [(o, c["created"], c["updated"], c["deleted"]) for o, c in sorted(counts.items())]
    table(("kind", "created", "updated", "deleted"), rows)


def band(score: str) -> str:
    return "low" if int(score) < 40 else "medium" if int(score) < 70 else "high"


def new_supplier(
    schema: Schema, code: str, name: str, country: str, score: str, **more: str
) -> Record:
    vat = f"{country}0000{code[2:]}"
    return build(
        schema,
        "Supplier",
        supplier_code=code,
        legal_name=name,
        country=country,
        vat_number=vat,
        risk_score=score,
        **more,
    )


# How the demo resolves each kind of Conflict: choice, custom value and the reason.
PLAN = {
    ConflictKind.ATTRIBUTE_VALUE: (ResolutionChoice.CUSTOM, "75", "analysts say 80, ERP says 45"),
    ConflictKind.DELETE_MODIFY: (ResolutionChoice.OURS, None, "the part is retired, keep it gone"),
    ConflictKind.ADD_ADD: (ResolutionChoice.OURS, None, "keep the analysts' Record"),
    ConflictKind.MISSING_VALUE: (ResolutionChoice.CUSTOM, "medium", "unscored until reviewed"),
}


def short(revision: bytes) -> str:
    return revision.hex()[:8]


def main() -> int:
    repo = Repo()

    print("== 1. main: the supply schema and its data")
    schema = load_schema((SUPPLY / "schema.yaml").read_text(encoding="utf-8"))
    print(
        f"schema: {len(schema.kinds)} Kinds, {len(schema.relations)} Relations, "
        f"{len(validate_schema(schema, RULES))} problems"
    )
    suppliers = [build(schema, "Supplier", **row) for row in read_csv("supplier")]
    parts = [build(schema, "Part", **row) for row in read_csv("part")]
    links = load_links(schema, "supplied_by")
    base = repo.edit(
        None,
        "Import suppliers and parts",
        "importer",
        schema=schema,
        put=[*suppliers, *parts, *links],
    )
    print(f"main {short(base)}: {len(suppliers)} Suppliers, {len(parts)} Parts, {len(links)} Links")
    main_head = base

    print("\n== 2. model/risk: analysts change the schema and the data")
    # Schema as code: dump to YAML, edit the document, load it on top of the old schema so
    # that existing elements keep their ids. New ids come from a counter, to stay deterministic.
    document = yaml.safe_load(dump_schema(schema))
    document["kinds"]["Supplier"]["attributes"]["risk_band"] = {
        "description": "Risk band derived from risk_score.",
        "type": {"enum": ["low", "medium", "high"]},
        "required": True,
    }
    counter = iter(range(1, 100))
    risk_schema = schema_from_document(
        document, base=schema, id_factory=lambda prefix: f"{prefix.value}_01KF{next(counter):022d}"
    )
    assert not validate_schema(risk_schema, RULES)
    supplier_kind, part_kind = (risk_schema.kind_by_api_name(n) for n in ("Supplier", "Part"))
    assert supplier_kind is not None
    assert part_kind is not None

    rescored = {"S-1001": "80", "S-1002": "55"}  # the other suppliers keep their score
    score_id = supplier_kind.attribute_by_api_name("risk_score").id  # type: ignore[union-attr]
    data = []
    for supplier in suppliers:
        score = rescored.get(supplier.key) or str(supplier.values[score_id])
        data.append(
            build(risk_schema, "Supplier", supplier, risk_score=score, risk_band=band(score))
        )
    data.append(new_supplier(risk_schema, "S-1031", "Norvik GmbH", "DE", "35", risk_band="low"))
    retired = "P-10003"
    gone = [where(link) for link in links if link.from_key == retired]
    risk = repo.edit(
        base,
        "Add risk_band, rescore, retire P-10003",
        "risk-analyst",
        schema=risk_schema,
        put=data,
        delete=[(kind_partition(part_kind.id), retired), *gone],
    )
    print(
        f"model/risk {short(risk)}: risk_band required and backfilled, S-1001 80, S-1002 55,"
        f" {retired} deleted, S-1031 added"
    )

    print("\n== 3. source/erp: the nightly feed changes data only")
    s1001, s1002 = (
        repo.record(repo.roots[base], supplier_kind.id, k) for k in ("S-1001", "S-1002")
    )
    part = repo.record(repo.roots[base], part_kind.id, retired)
    feed = [
        build(schema, "Supplier", s1001, risk_score="45"),
        build(schema, "Supplier", s1002, city="Warsaw"),
        build(schema, "Part", part, unit_cost="39.90"),
        new_supplier(schema, "S-1031", "Norvik Fasteners Sp. z o.o.", "DE", "50"),
        new_supplier(schema, "S-1032", "Orlane Gaskets s.r.o.", "CZ", "22"),
        new_supplier(schema, "S-1033", "Pelvar Hoses Ltd", "GB", "61"),
    ]
    erp = repo.edit(base, "ERP nightly sync", "erp-sync", put=feed)
    print(
        f"source/erp {short(erp)}: S-1001 score 45, S-1002 city, {retired} unit_cost, 3 Suppliers"
    )

    print("\n== 4. merge model/risk into main")
    outcome = repo.merge(main_head, risk)
    assert isinstance(outcome, MergedTree)
    main_head = risk  # main did not move since the fork, so the merge is a fast-forward
    print(
        f"{outcome.result.value}: {outcome.stats.changes} changes in "
        f"{outcome.stats.partitions_changed} partitions"
    )

    print("\n== 5. merge source/erp into main")
    names = id_names(read_tree_schema(repo.store, repo.objects, repo.roots[risk]))
    attempt = repo.merge(main_head, erp)
    assert isinstance(attempt, MergeAttempt)
    print(f"merge base {short(attempt.base or b'')}: {len(attempt.conflicts)} Conflicts")
    print_conflicts(attempt.conflicts, names)

    print("\n== 6. resolutions")
    resolutions = []
    for conflict in attempt.conflicts:
        choice, value, why = PLAN[conflict.kind]
        resolutions.append(
            Resolution(
                fingerprint=conflict.fingerprint, choice=choice, value=value, resolver="usr_antonio"
            )
        )
        print(f"  {conflict.kind.value} {describe(names, conflict)}: {choice.value}, {why}")

    print("\n== 7. merge again")
    merged = apply_resolutions(attempt, resolutions)
    assert isinstance(merged, MergedTree)
    head = repo.commit(merged.root, merged.parents, "Merge source/erp into main", "usr_antonio")
    print(
        f"{merged.result.value}: Revision {short(head)}, parents "
        f"{', '.join(short(p) for p in merged.parents)}"
    )
    print(
        f"{merged.stats.changes} changes in {merged.stats.partitions_changed} partitions, "
        "counts per Kind:"
    )
    print_counts(merged.changes, names)
    final = read_tree_schema(repo.store, repo.objects, merged.root)
    records = [
        (k.id, key)
        for k in final.kinds.values()
        for key, _ in tree_entries(repo.store, merged.root, kind_partition(k.id))
    ]
    issues = [
        i
        for kind_id, key in records
        for i in validate_record(final, repo.record(merged.root, kind_id, key))
    ]
    print(f"validation: {len(records)} Records against the merged schema, {len(issues)} issues")

    print("\n== 8. diff of two Revisions: the first import and merged main")
    diff = changeset_between(repo.store, repo.roots[base], merged.root)
    print(f"{short(base)} -> {short(head)}: {len(diff)} changes")
    print_counts(diff, names)
    for e in sorted(diff, key=lambda e: (owner(names, e.partition), e.key)):
        if e.old_hash is None or e.new_hash is None:  # elements that appear or disappear
            key = e.key.replace(LINK_KEY_SEPARATOR, " -> ")
            print(f"  {'+' if e.new_hash else '-'} {owner(names, e.partition)} {key}")
    before = repo.record(repo.roots[base], supplier_kind.id, "S-1001").values[score_id]
    after = repo.record(merged.root, supplier_kind.id, "S-1001").values[score_id]
    print(f"S-1001 risk_score: {before} -> {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
