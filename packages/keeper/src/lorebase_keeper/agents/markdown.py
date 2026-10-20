"""Markdown rendering of context packs and Record explanations, meant to be pasted into a prompt.

The output is stable (same input, same bytes) and compact: plain ASCII markup, lists for
Records and schema, tables for neighbors and changes, empty sections left out. Revisions
inside sections are shortened to `SHORT_REVISION` hex digits; the header carries the full id.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lorebase_core.model import Schema
from lorebase_keeper.agents.budget import Draft
from lorebase_keeper.agents.pack import links_json, schema_fragment

SHORT_REVISION = 12
SEPARATOR = "\n\n"


def short(revision: str | None) -> str:
    return "" if revision is None else revision[:SHORT_REVISION]


def text(value: Any) -> str:
    """A value on one line: strings as they are, lists comma-separated, objects as JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(text(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return " ".join(str(value).split())


def cell(value: Any) -> str:
    """A value inside a table cell."""
    return text(value).replace("|", "\\|")


def pairs(values: Mapping[str, Any] | None) -> str:
    return "; ".join(f"{name}={text(value)}" for name, value in (values or {}).items())


def table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
    return lines


def heading(ref: str, title: str | None) -> str:
    return f"### {ref}" + (f": {text(title)}" if title else "")


# Context pack sections ----------------------------------------------------------------------


def _seeds(draft: Draft) -> str:
    lines = ["## Records"]
    for seed in draft.seeds:
        lines.append(heading(seed.ref, seed.title))
        lines.extend(f"- {name}: {text(value)}" for name, value in seed.values.items())
        if seed.updated_revision is not None:
            lines.append(f"- (last changed in {short(seed.updated_revision)})")
    return "\n".join(lines)


def _neighbors(draft: Draft) -> str:
    if not draft.neighbors:
        return ""
    rows = [
        (n.ref, n.title, n.depth, "(omitted)" if n.values is None else pairs(n.values))
        for n in draft.neighbors
    ]
    return "\n".join(["## Neighbors", *table(("ref", "title", "depth", "values"), rows)])


def _links(draft: Draft, schema: Schema) -> str:
    if not draft.links:
        return ""
    lines = ["## Links"]
    for name, links in links_json(draft.links).items():
        found = schema.relation_by_api_name(name)
        ends = ""
        if found is not None:
            relation = found[0]
            source, target = (
                schema.kinds.get(relation.from_kind),
                schema.kinds.get(relation.to_kind),
            )
            if source is not None and target is not None:
                ends = f" ({source.api_name} -> {target.api_name})"
        lines.append(f"### {name}{ends}")
        for link in links:
            extra = f" ({pairs(link['values'])})" if link.get("values") else ""
            lines.append(f"- {link['from']} -> {link['to']}{extra}")
    return "\n".join(lines)


def _attribute_line(attribute: Mapping[str, Any], indent: str = "") -> str:
    flags = [attribute["type"]]
    if attribute.get("required"):
        flags.append("required")
    if attribute.get("derived"):
        flags.append("derived")
    description = attribute.get("description")
    return f"{indent}- {attribute['name']} ({', '.join(flags)})" + (
        f": {text(description)}" if description else ""
    )


def schema_markdown(fragment: Mapping[str, Any]) -> str:
    lines = ["## Schema"]
    for kind in fragment["kinds"]:
        lines.append(f"### Kind {kind['name']}")
        if kind.get("description"):
            lines.append(text(kind["description"]))
        keys = f"key: {', '.join(kind['key'])}"
        lines.append(keys + (f"; title: {kind['title']}" if kind.get("title") else ""))
        lines.extend(_attribute_line(a) for a in kind["attributes"])
    if fragment["relations"]:
        lines.append("### Relations")
        for relation in fragment["relations"]:
            line = (
                f"- {relation['name']}: {relation['from']} -> {relation['to']}, "
                f"{relation['cardinality']}, inverse {relation['inverse']}"
            )
            if relation.get("description"):
                line += f". {text(relation['description'])}"
            lines.append(line)
            lines.extend(_attribute_line(a, "  ") for a in relation.get("attributes", ()))
    return "\n".join(lines)


def _changes(draft: Draft) -> str:
    if not draft.changes:
        return ""
    rows = [
        (
            short(c.revision),
            c.at,
            c.ref,
            c.change,
            ", ".join(c.attributes),
            c.actor,
            c.origin,
            c.message,
        )
        for c in draft.changes
    ]
    header = ("revision", "at", "record", "change", "attributes", "by", "origin", "message")
    return "\n".join(["## Recent changes", *table(header, rows)])


def _lineage(draft: Draft) -> str:
    if not draft.lineage:
        return ""
    lines = ["## Lineage"]
    for item in draft.lineage:
        source = " ".join(
            part
            for part in (
                item.origin or "unknown origin",
                f"by {item.actor}" if item.actor else "",
                f"at {item.at}" if item.at else "",
            )
            if part
        )
        lines.append(f"- {item.ref}: {', '.join(item.attributes)} <- {source}")
    return "\n".join(lines)


def markdown_sections(draft: Draft, schema: Schema) -> dict[str, str]:
    """Text of each section, in output order; empty for sections without content."""
    return {
        "seeds": _seeds(draft),
        "neighbors": _neighbors(draft),
        "links": _links(draft, schema),
        "schema": schema_markdown(schema_fragment(draft, schema)) if draft.schema else "",
        "recent_changes": _changes(draft),
        "lineage": _lineage(draft),
    }


def markdown_measure(schema: Schema) -> Any:
    """Characters of each section including its separator, a `budget.Measure`."""

    def measure(draft: Draft) -> Mapping[str, int]:
        return {
            name: len(body) + len(SEPARATOR) if body else 0
            for name, body in markdown_sections(draft, schema).items()
        }

    return measure


def pack_markdown(
    sections: Mapping[str, str],
    *,
    domain: str,
    branch: str,
    revision: str,
    budget: Mapping[str, Any],
    since: str | None,
) -> str:
    """The whole pack: a header line, then the non-empty sections."""
    trimmed = ", ".join(f"{t['step']}({t['removed']})" for t in budget["trimmed"]) or "none"
    header = [
        f"# Context: {domain}/{branch}",
        f"revision {revision}; estimated {budget['used_tokens']} of "
        f"{budget['limit_tokens']} tokens; trimmed: {trimmed}"
        + ("; over budget" if budget["over_budget"] else "")
        + (f"; changes since {short(since)}" if since else ""),
    ]
    body = [part for part in sections.values() if part]
    return SEPARATOR.join(["\n".join(header), *body]) + "\n"
