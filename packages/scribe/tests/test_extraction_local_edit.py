"""A value item over a value a person set last is never accepted at once (design 9.1): it
stays pending with hold reason `local_edit`."""

from __future__ import annotations

from datetime import UTC, datetime

from lorebase_core.model import Schema
from lorebase_scribe.extraction.assemble import CurrentState, assemble_items, by_person
from lorebase_scribe.extraction.candidates import (
    Evidence,
    Mention,
    Resolution,
    Resolved,
    ValueCandidate,
)
from lorebase_scribe.extraction.ontology import build_ontology
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.models.enums import ItemDecision

NOW = datetime(2026, 10, 29, tzinfo=UTC)
SETTINGS = ExtractionSettings(auto_accept_threshold=0.6)


def _ids(schema: Schema, name: str) -> tuple[str, str]:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    attribute = kind.attribute_by_api_name(name)
    assert attribute is not None
    return kind.id, attribute.id


def _known(kind_id: str, key: str) -> Resolved:
    mention = Mention(
        kind_id=kind_id,
        text=key,
        start=0,
        end=len(key),
        chunk=0,
        score=1.0,
        tier=0,
        source="key",
        extractor="test",
        document_id="doc_1",
    )
    return Resolved(mention, Resolution(kind_id, "matched", key, 1.0, "exact"))


def test_values_a_person_set_stay_pending(supply_schema: Schema) -> None:
    kind_id, city = _ids(supply_schema, "city")
    _kind, legal = _ids(supply_schema, "legal_name")
    known = _known(kind_id, "S-1013")
    evidence = Evidence(document_id="doc_1", chunk=0, start=0, end=4, quote="Graz")
    values = [
        ValueCandidate(known, city, "Graz", evidence, 1.0, 0, "rules"),
        ValueCandidate(known, legal, "Kavsen Alloys AG", evidence, 1.0, 0, "rules"),
    ]
    assembled = assemble_items(
        build_ontology(supply_schema, None),
        [known],
        values,
        [],
        CurrentState(
            records={(kind_id, "S-1013"): {city: "Vienna", legal: "Kavsen Alloys"}},
            links=frozenset(),
            personal=frozenset({(kind_id, "S-1013", city)}),
        ),
        job_id="exj_1",
        principal="svc_scribe",
        now=NOW,
        settings=SETTINGS,
    )
    decisions = {i.attribute_id: i.decision for i in assembled.items}
    assert decisions == {city: ItemDecision.PENDING, legal: ItemDecision.ACCEPTED}
    assert assembled.stats["held_local_edit"] == 1
    held = next(i for i in assembled.items if i.attribute_id == city)
    assert held.match is not None
    assert held.match["hold"] == "local_edit"


def test_who_counts_as_a_person() -> None:
    assert by_person("operation", {"principal": "usr_1"})
    assert by_person("merge_resolution", {"resolver": "usr_1"})
    assert by_person("reconciliation", {"resolver": "principal:usr_1"})
    assert not by_person("merge_resolution", {"resolver": "policy:prefer"})
    assert not by_person("reconciliation", {"resolver": "policy:newest_wins"})
    assert not by_person("source_sync", {"source_id": "src_1"})
    assert not by_person("extraction", {})
