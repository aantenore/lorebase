"""Markdown rendering of health reports, meant to be pasted into a prompt.

Stable (same report, same bytes) and compact: plain ASCII, one table of Kinds, and a section
only for what has something to report. Revisions are shortened to `SHORT_REVISION` digits.
"""

from __future__ import annotations

from collections.abc import Sequence

from lorebase_keeper.kindhealth.models import (
    DomainHealthOut,
    KindHealth,
    KindHealthOut,
    RelationHealth,
)

SHORT_REVISION = 12


def _row(cells: Sequence[object]) -> str:
    return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    return [_row(header), "|" + " --- |" * len(header), *(_row(r) for r in rows)]


def _verdict(ok: bool) -> str:
    return "ok" if ok else "needs attention"


def _kinds_table(kinds: Sequence[KindHealth]) -> list[str]:
    rows = [
        (
            k.kind,
            k.records,
            k.violations.invalid_records,
            k.duplicates.total_groups,
            "stale" if k.derived is not None and k.derived.stale else "",
            "" if k.last_change is None else k.last_change.revision[:SHORT_REVISION],
        )
        for k in kinds
    ]
    return _table(
        ("kind", "records", "invalid", "duplicate groups", "derived", "last change"), rows
    )


def _kind_details(kind: KindHealth) -> list[str]:
    lines: list[str] = []
    violations = kind.violations
    if violations.issues:
        scope = f"{violations.checked} of {kind.records} Records checked" + (
            " (sample)" if violations.sampled else ""
        )
        lines += [f"### {kind.kind}: violations ({scope})"]
        for issue in violations.issues:
            where = f" on {issue.attribute}" if issue.attribute else ""
            lines.append(f"- {issue.code}{where}: {issue.count} (e.g. {', '.join(issue.examples)})")
        if violations.issues_truncated:
            lines.append("- (more kinds of violation omitted)")
    duplicates = kind.duplicates
    if duplicates.groups or duplicates.hidden:
        lines += [f"### {kind.kind}: duplicates ({duplicates.total_groups} groups)"]
        for group in duplicates.groups:
            value = "" if group.value is None else f' "{group.value}"'
            flag = " [unique]" if group.unique else ""
            lines.append(
                f"- {group.attribute}{value}{flag}: {', '.join(group.keys)} ({group.count})"
            )
        for withheld in duplicates.hidden:
            flag = " [unique]" if withheld.unique else ""
            lines.append(
                f"- {withheld.attribute}{flag}: {withheld.groups} groups, {withheld.records} "
                "Records (not listed: sensitive)"
            )
    elif duplicates.skipped:
        lines.append(f"### {kind.kind}: duplicates not checked ({duplicates.skipped})")
    if kind.derived is not None and kind.derived.stale:
        names = ", ".join(kind.derived.attributes)
        lines.append(
            f"### {kind.kind}: {kind.derived.records_missing_value} Records lack derived "
            f"values ({names})"
        )
    return lines


def _relations(relations: Sequence[RelationHealth]) -> list[str]:
    rows = [
        (
            r.relation,
            f"{r.from_kind} -> {r.to_kind}",
            r.links,
            r.orphan_links.count,
            "" if r.required_gaps is None else r.required_gaps.missing,
            r.direction or "",
        )
        for r in relations
    ]
    lines = [
        "## Relations",
        *_table(("relation", "ends", "links", "orphans", "gaps", "side"), rows),
    ]
    for item in relations:
        if item.orphan_links.examples:
            pairs = ", ".join(f"{e.from_key}->{e.to_key}" for e in item.orphan_links.examples)
            lines.append(f"- {item.relation} orphans: {pairs}")
        if item.required_gaps is not None and item.required_gaps.missing:
            lines.append(
                f"- {item.relation} required, missing for: {', '.join(item.required_gaps.examples)}"
            )
    return lines


def _summary(report: DomainHealthOut | KindHealthOut) -> list[str]:
    s = report.summary
    return [
        f"{_verdict(s.ok)}: {s.kinds} kinds, {s.records} records, {s.links} links",
        f"- invalid records: {s.invalid_records}; orphan links: {s.orphan_links}; "
        f"required-relation gaps: {s.required_relation_gaps}; duplicate groups: "
        f"{s.duplicate_groups}; kinds with stale derived values: {s.stale_kinds}",
    ]


def domain_markdown(report: DomainHealthOut) -> str:
    """A Domain report as markdown."""
    lines = [
        f"# Health of {report.domain} on {report.branch}",
        f"revision {report.revision[:SHORT_REVISION]}",
        "",
        *_summary(report),
        "",
        "## Kinds",
        *_kinds_table(report.kinds),
    ]
    for kind in report.kinds:
        details = _kind_details(kind)
        if details:
            lines += ["", *details]
    if report.relations:
        lines += ["", *_relations(report.relations)]
    return "\n".join(lines) + "\n"


def kind_markdown(report: KindHealthOut) -> str:
    """A Kind report as markdown."""
    kind = report.health
    lines = [
        f"# Health of {kind.kind} in {report.domain} on {report.branch}",
        f"revision {report.revision[:SHORT_REVISION]}",
        "",
        *_summary(report),
        "",
        *_kinds_table([kind]),
    ]
    details = _kind_details(kind)
    if details:
        lines += ["", *details]
    if report.relations:
        lines += ["", *_relations(report.relations)]
    return "\n".join(lines) + "\n"
