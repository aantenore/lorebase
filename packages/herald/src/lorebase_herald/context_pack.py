"""Context packs for `lorebase_context`, composed from Keeper reads within a token budget.

Keeper has no context endpoint yet, so Herald composes the pack from the seed Records (with
their Link counts), their neighborhood and the schema of the Kinds involved. Sections are
added in priority order (seeds, links to neighbors, schema, neighbor values) until the budget
is spent; whatever does not fit is counted in a closing `[context trimmed ...]` line. Budgets
are counted in characters, `token_budget * chars_per_token`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lorebase_herald import views
from lorebase_herald.render import as_json, ref, titled, values_text
from lorebase_herald.settings import OutputSettings


@dataclass
class Section:
    title: str
    lines: list[str]


@dataclass
class ContextPack:
    schema: Mapping[str, Any]
    neighborhood: Mapping[str, Any]
    seeds: Sequence[Mapping[str, Any]]
    found_by: str | None
    question: str | None
    output: OutputSettings

    def _parts(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        seed_views = [views.record_view(r) for r in self.seeds]
        seed_refs = {v["ref"] for v in seed_views}
        neighbors = [
            views.record_view(node)
            for node in self.neighborhood.get("nodes") or []
            if ref(node["kind"], node["key"]) not in seed_refs
        ]
        edges = [views.edge_view(e) for e in self.neighborhood.get("edges") or []]
        return seed_views, neighbors, edges

    def _kinds(
        self, seeds: Sequence[Mapping[str, Any]], neighbors: Sequence[Mapping[str, Any]]
    ) -> list[str]:
        names: list[str] = []
        for view in (*seeds, *neighbors):
            kind = view["ref"].split("/", 1)[0]
            if kind not in names:
                names.append(kind)
        return names

    def _header(self) -> str:
        revision = self.neighborhood.get("revision") or self.schema.get("revision")
        how = f" (seeds found by search for {self.question!r})" if self.found_by else ""
        return (
            f"context {self.schema.get('domain')}@{self.schema.get('branch')} rev {revision}{how}"
        )

    def text(self, max_chars: int) -> str:
        seeds, neighbors, edges = self._parts()
        titles = {v["ref"]: v.get("title") for v in (*seeds, *neighbors)}
        limit = self.output.max_value_chars
        seed_lines = [views.record_text(view, None, self.output) for view in seeds]
        schema_view = views.schema_view(
            self.schema, kinds=self._kinds(seeds, neighbors), detail="summary"
        )
        sections = [
            Section("SEEDS", seed_lines),
            Section("LINKS", [views.edge_line(e, titles, limit) for e in edges]),
            Section("SCHEMA (kinds involved)", views.involved_schema_lines(schema_view)),
            Section(
                "NEIGHBORS",
                [
                    f"{titled(n['ref'], n.get('title'))} | {values_text(n['values'], limit)}"
                    for n in neighbors
                ],
            ),
        ]
        return _fit(self._header(), sections, max_chars, self.neighborhood.get("truncated"))

    def json(self, max_chars: int) -> str:
        seeds, neighbors, edges = self._parts()
        schema_view = views.schema_view(
            self.schema, kinds=self._kinds(seeds, neighbors), detail="summary"
        )
        pack: dict[str, Any] = {
            "domain": self.schema.get("domain"),
            "branch": self.schema.get("branch"),
            "revision": self.neighborhood.get("revision") or self.schema.get("revision"),
            "seeds": seeds,
            "edges": edges,
            "schema": {"kinds": schema_view["kinds"], "relations": schema_view["relations"]},
            "neighbors": neighbors,
        }
        dropped = {"neighbors": 0, "edges": 0}
        # Lowest priority first: neighbor values, then neighbors, then edges.
        while len(as_json(pack)) > max_chars and (pack["neighbors"] or pack["edges"]):
            if pack["neighbors"]:
                pack["neighbors"].pop()
                dropped["neighbors"] += 1
            else:
                pack["edges"].pop()
                dropped["edges"] += 1
        if any(dropped.values()):
            pack["trimmed"] = dropped
        return as_json(pack)


def _fit(header: str, sections: Sequence[Section], max_chars: int, capped: Any) -> str:
    """Sections in order while they fit; a closing line counts what was left out."""
    lines = [header]
    used = len(header)
    omitted: dict[str, int] = {}
    full = False
    for section in sections:
        if not section.lines:
            continue
        if full:
            omitted[section.title] = len(section.lines)
            continue
        title = section.title + ":"
        if used + len(title) + 1 > max_chars:
            full = True
            omitted[section.title] = len(section.lines)
            continue
        lines.append(title)
        used += len(title) + 1
        for index, line in enumerate(section.lines):
            if used + len(line) + 1 > max_chars:
                full = True
                omitted[section.title] = len(section.lines) - index
                break
            lines.append(line)
            used += len(line) + 1
    if capped:
        lines.append("neighborhood capped by Keeper; narrow `relations` for other links")
    if omitted:
        left_out = ", ".join(
            f"{count} {name.split(' ')[0].lower()} lines" for name, count in omitted.items()
        )
        lines.append(
            f"[context trimmed to budget: {left_out} left out; raise token_budget or use "
            "lorebase_get / lorebase_neighborhood]"
        )
    return "\n".join(lines)
