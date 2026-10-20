"""``TreeIndexView`` answers ``IndexView`` questions from a Revision tree."""

from __future__ import annotations

from lorebase_core.canonical import ObjectType
from lorebase_core.merge import InMemoryObjectStore, TreeIndexView
from lorebase_core.model import Link, Record
from lorebase_core.tree import InMemoryNodeStore, TreeChange, kind_partition, link_key, tree_apply
from lorebase_core.tree import relation_partition as links_of

KIND = "knd_01JC0M00000000000000000001"
OTHER = "knd_01JC0M00000000000000000002"
RELATION = "rel_01JC0M00000000000000000003"
VAT = "att_01JC0M00000000000000000004"
REFS = "att_01JC0M00000000000000000005"


def build() -> TreeIndexView:
    store, objects = InMemoryNodeStore(), InMemoryObjectStore()
    records = [
        Record(kind=KIND, key="A", values={VAT: "IT1", REFS: ["X", "Y"]}),
        Record(kind=KIND, key="B", values={VAT: "IT2", REFS: "X"}),
    ]
    links = [
        Link(relation=RELATION, from_key="A", to_key="X"),
        Link(relation=RELATION, from_key="A", to_key="Y"),
        Link(relation=RELATION, from_key="B", to_key="X"),
    ]
    changes = [
        TreeChange(kind_partition(r.kind), r.key, objects.put(ObjectType.RECORD, r.content()))
        for r in records
    ] + [
        TreeChange(
            links_of(link.relation),
            link_key(link.from_key, link.to_key),
            objects.put(ObjectType.LINK, link.content()),
        )
        for link in links
    ]
    return TreeIndexView(store, objects, tree_apply(store, None, changes))


def test_records_and_unique_values() -> None:
    view = build()
    assert view.record_exists(KIND, "A")
    assert not view.record_exists(KIND, "C")
    assert not view.record_exists(OTHER, "A")
    assert view.find_unique(KIND, VAT, "IT2") == "B"
    assert view.find_unique(KIND, VAT, "IT9") is None
    assert list(view.keys_referencing(KIND, REFS, "X")) == ["A", "B"]


def test_links() -> None:
    view = build()
    assert view.link_exists(RELATION, "A", "X")
    assert not view.link_exists(RELATION, "B", "Y")
    assert view.count_links(RELATION, from_key="A") == 2
    assert view.count_links(RELATION, to_key="X") == 2
    assert list(view.links_of(RELATION, from_key="A", to_key="Y")) == [("A", "Y")]
    assert view.count_links(RELATION) == 3
