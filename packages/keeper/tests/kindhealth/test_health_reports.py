"""Health report assembly and rendering, without a database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lorebase_core.model.yaml_schema import load_schema
from lorebase_keeper.kindhealth import markdown, report
from lorebase_keeper.kindhealth.config import HealthSettings
from lorebase_keeper.kindhealth.models import (
    DerivedReport,
    DomainHealthOut,
    DuplicateGroup,
    DuplicateReport,
    IssueGroup,
    KindHealth,
    KindHealthOut,
    LastChange,
    LinkRef,
    OrphanLinks,
    RelationHealth,
    RequiredGaps,
    ViolationReport,
)
from lorebase_keeper.reading.points import KindScope

SCHEMA = load_schema(
    (Path(__file__).resolve().parents[4] / "examples" / "supply" / "schema.yaml").read_text()
)
REVISION = "ab" * 32


def point(kinds: set[str] | None) -> Any:
    scope = KindScope(guard=None, kinds=None if kinds is None else frozenset(kinds))  # type: ignore[arg-type]
    return SimpleNamespace(schema=SCHEMA, scope=scope)


def kind_health(name: str, **changes: Any) -> KindHealth:
    base: dict[str, Any] = {
        "kind": name,
        "records": 10,
        "violations": ViolationReport(checked=10, sampled=False, invalid_records=0, issues=[]),
        "duplicates": DuplicateReport(attributes=["title"], groups=[], total_groups=0),
    }
    return KindHealth(**{**base, **changes})


def relation_health(name: str = "supplied_by", **changes: Any) -> RelationHealth:
    base: dict[str, Any] = {
        "relation": name,
        "from_kind": "Part",
        "to_kind": "Supplier",
        "backing": "explicit",
        "links": 5,
        "orphan_links": OrphanLinks(count=0, missing_from=0, missing_to=0, examples=[]),
        "required": False,
    }
    return RelationHealth(**{**base, **changes})


def test_the_defaults_cap_the_work_of_one_request() -> None:
    settings = HealthSettings()
    assert settings.max_records_validated == 5000
    assert settings.duplicates_max_records == 200_000
    assert settings.statement_timeout_ms == 15_000


def test_only_kinds_and_relations_the_caller_may_read_are_examined() -> None:
    everything = point(None)
    assert [k.api_name for k in report.visible_kinds(everything)] == [
        "Contract",
        "Part",
        "Plant",
        "PurchaseOrder",
        "RiskFlag",
        "Supplier",
    ]
    assert len(report.visible_relations(everything)) == len(SCHEMA.relations)

    narrow = point({"Part", "Supplier"})
    assert [k.api_name for k in report.visible_kinds(narrow)] == ["Part", "Supplier"]
    assert [r.api_name for r in report.visible_relations(narrow)] == ["supplied_by"]

    supplier = SCHEMA.kind_by_api_name("Supplier")
    assert supplier is not None
    assert [r.api_name for r in report.visible_relations(everything, supplier)] == [
        "flags",
        "issued_to",
        "supplied_by",
        "with_supplier",
    ]
    assert report.visible_relations(point({"Supplier"}), supplier) == []


def test_the_summary_counts_findings_and_decides_whether_all_is_well() -> None:
    clean = report.summarize([kind_health("Part")], [relation_health()])
    assert clean.ok
    assert (clean.kinds, clean.records, clean.links) == (1, 10, 5)

    hints = kind_health(
        "Part",
        duplicates=DuplicateReport(
            attributes=["name"],
            groups=[DuplicateGroup(attribute="name", unique=False, value="x", count=2, keys=["a"])],
            total_groups=1,
        ),
        derived=DerivedReport(attributes=["supplier_count"], records_missing_value=10, stale=True),
    )
    summary = report.summarize([hints], [])
    assert summary.ok  # duplicate titles and stale derived values are hints
    assert (summary.duplicate_groups, summary.stale_kinds) == (1, 1)

    unique = kind_health(
        "Supplier",
        duplicates=DuplicateReport(
            attributes=["vat_number"],
            groups=[
                DuplicateGroup(attribute="vat_number", unique=True, value=None, count=2, keys=[])
            ],
            total_groups=1,
        ),
    )
    assert not report.summarize([unique], []).ok

    invalid = kind_health(
        "Part",
        violations=ViolationReport(checked=4, sampled=True, invalid_records=2, issues=[]),
    )
    broken = report.summarize(
        [invalid],
        [
            relation_health(
                orphan_links=OrphanLinks(count=3, missing_from=1, missing_to=2, examples=[]),
                required=True,
                required_gaps=RequiredGaps(missing=4, examples=[]),
            )
        ],
    )
    assert not broken.ok
    assert (broken.invalid_records, broken.orphan_links, broken.required_relation_gaps) == (2, 3, 4)
    assert broken.sampled_kinds == 1


def test_a_kind_report_marks_the_side_of_each_relation() -> None:
    part = SCHEMA.kind_by_api_name("Part")
    assert part is not None
    marked = report.with_direction(
        [
            relation_health("supplied_by"),
            relation_health("for_part", from_kind="PurchaseOrder", to_kind="Part"),
            relation_health("successor", from_kind="Part", to_kind="Part"),
        ],
        part,
    )
    assert [r.direction for r in marked] == ["outgoing", "incoming", "self"]


def domain_report() -> DomainHealthOut:
    part = kind_health(
        "Part",
        last_change=LastChange(revision=REVISION, at=datetime(2026, 10, 1, 8, tzinfo=UTC)),
        violations=ViolationReport(
            checked=10,
            sampled=True,
            invalid_records=2,
            issues=[
                IssueGroup(
                    code="value.pattern", attribute="part_number", count=2, examples=["x1", "x2"]
                )
            ],
        ),
        duplicates=DuplicateReport(
            attributes=["name"],
            groups=[
                DuplicateGroup(
                    attribute="name", unique=False, value="flange", count=2, keys=["P-1", "P-2"]
                )
            ],
            total_groups=1,
        ),
        derived=DerivedReport(attributes=["supplier_count"], records_missing_value=10, stale=True),
    )
    relations = [
        relation_health(
            "stocked_at",
            to_kind="Plant",
            orphan_links=OrphanLinks(
                count=1,
                missing_from=0,
                missing_to=1,
                examples=[LinkRef(from_key="P-1", to_key="PL-01")],
            ),
        ),
        relation_health(
            "for_part",
            from_kind="PurchaseOrder",
            to_kind="Part",
            required=True,
            required_gaps=RequiredGaps(missing=1, examples=["PO-1"]),
        ),
    ]
    return DomainHealthOut(
        revision=REVISION,
        served_from="materialized",
        domain="supply",
        branch="main",
        summary=report.summarize([part], relations),
        kinds=[part],
        relations=relations,
    )


def test_the_markdown_for_agents_is_compact_and_stable() -> None:
    first = markdown.domain_markdown(domain_report())
    assert first == markdown.domain_markdown(domain_report())
    assert first == (
        "# Health of supply on main\n"
        "revision abababababab\n"
        "\n"
        "needs attention: 1 kinds, 10 records, 10 links\n"
        "- invalid records: 2; orphan links: 1; required-relation gaps: 1; duplicate groups: 1; "
        "kinds with stale derived values: 1\n"
        "\n"
        "## Kinds\n"
        "| kind | records | invalid | duplicate groups | derived | last change |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| Part | 10 | 2 | 1 | stale | abababababab |\n"
        "\n"
        "### Part: violations (10 of 10 Records checked (sample))\n"
        "- value.pattern on part_number: 2 (e.g. x1, x2)\n"
        "### Part: duplicates (1 groups)\n"
        '- name "flange": P-1, P-2 (2)\n'
        "### Part: 10 Records lack derived values (supplier_count)\n"
        "\n"
        "## Relations\n"
        "| relation | ends | links | orphans | gaps | side |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| stocked_at | Part -> Plant | 5 | 1 |  |  |\n"
        "| for_part | PurchaseOrder -> Part | 5 | 0 | 1 |  |\n"
        "- stocked_at orphans: P-1->PL-01\n"
        "- for_part required, missing for: PO-1\n"
    )


def test_a_sound_domain_renders_without_detail_sections() -> None:
    summary = report.summarize([kind_health("Part")], [])
    out = DomainHealthOut(
        revision=REVISION,
        served_from="materialized",
        domain="supply",
        branch="main",
        summary=summary,
        kinds=[kind_health("Part")],
        relations=[],
    )
    text = markdown.domain_markdown(out)
    assert "ok: 1 kinds" in text
    assert "###" not in text
    assert "## Relations" not in text


def test_a_kind_report_renders_its_relations() -> None:
    health = kind_health("Plant")
    relations = [relation_health("stocked_at", to_kind="Plant", direction="incoming")]
    out = KindHealthOut(
        revision=REVISION,
        served_from="materialized",
        domain="supply",
        branch="main",
        summary=report.summarize([health], relations),
        health=health,
        relations=relations,
    )
    text = markdown.kind_markdown(out)
    assert text.startswith("# Health of Plant in supply on main\n")
    assert "| stocked_at | Part -> Plant | 5 | 0 |  | incoming |" in text


@pytest.mark.parametrize("skipped", ["too_many_records", "disabled"])
def test_skipped_duplicate_checks_are_said_so(skipped: str) -> None:
    health = kind_health(
        "Part",
        duplicates=DuplicateReport(attributes=["name"], groups=[], total_groups=0, skipped=skipped),  # type: ignore[arg-type]
    )
    out = DomainHealthOut(
        revision=REVISION,
        served_from="materialized",
        domain="supply",
        branch="main",
        summary=report.summarize([health], []),
        kinds=[health],
        relations=[],
    )
    assert f"### Part: duplicates not checked ({skipped})" in markdown.domain_markdown(out)
