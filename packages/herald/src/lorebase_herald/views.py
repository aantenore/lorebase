"""Typed Keeper answers turned into compact views (the `json` format) and their text form.

Views keep api names and `Kind/key` refs, drop internal ids, and always carry the Revision a
response was served from. Lists are capped at `output.max_items` with an explicit count of what
was left out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lorebase_client import models as m
from lorebase_herald.render import (
    as_json,
    capped,
    more_line,
    ref,
    timestamp,
    titled,
    value_text,
    values_text,
)
from lorebase_herald.settings import OutputSettings

GENERIC_OPERATIONS = (
    "import_records(kind, rows)",
    "import_links(relation, rows)",
    "delete_records(kind, keys)",
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

View = dict[str, Any]


def snake_case(name: str) -> str:
    """`PurchaseOrder` -> `purchase_order`, as Keeper names built-in Operations."""
    return _CAMEL.sub("_", name).lower()


# Schema --------------------------------------------------------------------------------------


def type_text(spec: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """A type spec in one token-lean string: `enum(a|b)`, `ref(Supplier)`, `decimal(10,2)`."""
    name = str(spec.get("name", "?"))
    if "enum_values" in spec:
        return f"enum({'|'.join(str(v) for v in spec['enum_values'])})"
    if "kind_ref" in spec:
        return f"ref({names.get(spec['kind_ref'], spec['kind_ref'])})"
    args: list[str] = []
    for key, value in spec.items():
        if key == "name":
            continue
        if isinstance(value, Mapping) and "name" in value:
            args.append(type_text(value, names))
        elif isinstance(value, list):
            args.append("|".join(str(v) for v in value))
        elif key in ("precision", "scale"):
            args.append(str(value))
        else:
            args.append(f"{key}={value}")
    return f"{name}({','.join(args)})" if args else name


def _attributes(attributes: Mapping[str, Any], names: Mapping[str, str], full: bool) -> list[View]:
    out: list[View] = []
    for attribute in attributes.values():
        item: View = {
            "name": attribute["api_name"],
            "type": type_text(attribute.get("type") or {}, names),
        }
        if attribute.get("required"):
            item["required"] = True
        if attribute.get("derivation_id"):
            item["derived"] = True
        if full and attribute.get("description"):
            item["description"] = attribute["description"]
        out.append(item)
    return out


def schema_view(document: m.SchemaDocument, *, kinds: Sequence[str] | None, detail: str) -> View:
    """Kinds, Relations and Operations of a schema document (`GET .../schema`)."""
    content: Mapping[str, Any] = document.schema_
    full = detail == "full"
    names = {
        element_id: element["api_name"]
        for section in ("kinds", "traits", "relations")
        for element_id, element in (content.get(section) or {}).items()
    }
    wanted = set(kinds or ())
    kind_views: list[View] = []
    for kind in (content.get("kinds") or {}).values():
        if wanted and kind["api_name"] not in wanted:
            continue
        attribute_names = {a_id: a["api_name"] for a_id, a in kind["attributes"].items()}
        view: View = {
            "name": kind["api_name"],
            "description": kind.get("description") or "",
            "key": [attribute_names.get(a, a) for a in kind.get("primary_key") or []],
            "attributes": _attributes(kind["attributes"], names, full),
            "operations": [
                f"{op}_{snake_case(kind['api_name'])}"
                for op in kind.get("operations_builtin") or []
            ],
        }
        if kind.get("title_attribute"):
            view["title"] = attribute_names.get(kind["title_attribute"], kind["title_attribute"])
        traits = [names.get(t["trait_id"], t["trait_id"]) for t in kind.get("traits") or []]
        if traits:
            view["traits"] = traits
        if kind.get("status") not in (None, "active"):
            view["status"] = kind["status"]
        kind_views.append(view)
    relation_views: list[View] = []
    for relation in (content.get("relations") or {}).values():
        source = names.get(relation["from_kind"], relation["from_kind"])
        target = names.get(relation["to_kind"], relation["to_kind"])
        if wanted and source not in wanted and target not in wanted:
            continue
        view = {
            "name": relation["api_name"],
            "inverse": relation.get("inverse_api_name"),
            "from": source,
            "to": target,
            "cardinality": relation.get("cardinality"),
            "description": relation.get("description") or "",
        }
        if relation.get("attributes"):
            view["attributes"] = _attributes(relation["attributes"], names, full)
        if relation.get("backing") == "explicit":
            view["operations"] = [f"link_{relation['api_name']}", f"unlink_{relation['api_name']}"]
        else:
            view["backing"] = relation.get("backing")
        relation_views.append(view)
    operation_views: list[View] = []
    for operation in (content.get("operations") or {}).values():
        parameters = []
        for parameter in operation.get("parameters") or []:
            item: View = {
                "name": parameter["name"],
                "type": type_text(parameter.get("type") or {}, names),
            }
            if parameter.get("required"):
                item["required"] = True
            if full and parameter.get("description"):
                item["description"] = parameter["description"]
            parameters.append(item)
        view = {
            "name": operation["api_name"],
            "description": operation.get("description") or "",
            "parameters": parameters,
            "review": (operation.get("review_policy") or {}).get("mode", "never"),
        }
        if full and (operation.get("review_policy") or {}).get("expression"):
            view["review_when"] = operation["review_policy"]["expression"]
        operation_views.append(view)
    lens_views = [lens_view(lens, names) for lens in (content.get("lenses") or {}).values()]
    return {
        "domain": document.domain,
        "branch": document.branch,
        "revision": document.revision,
        "kinds": kind_views,
        "relations": relation_views,
        "operations": operation_views,
        "generic_operations": list(GENERIC_OPERATIONS),
        **({"lenses": lens_views} if lens_views else {}),
        "search": {
            "modes": list(document.search.modes),
            "embedding": (
                None
                if document.search.embedding is None
                else {
                    "model": document.search.embedding.model,
                    "dims": document.search.embedding.dims,
                }
            ),
        },
    }


def lens_view(lens: Mapping[str, Any], names: Mapping[str, str]) -> View:
    """A lens of a schema document with its references written as api names."""
    view: View = {"name": lens["api_name"], "description": lens.get("description") or ""}
    for section in ("kinds", "traits", "relations"):
        if lens.get(section):
            view[section] = [names.get(element_id, element_id) for element_id in lens[section]]
    for member in (
        "depth",
        "include_schema",
        "include_recent_changes",
        "include_lineage",
        "budget_tokens",
    ):
        if lens.get(member) is not None:
            view[member] = lens[member]
    return view


def lens_text(view: Mapping[str, Any]) -> str:
    """One line per member of a lens, for the lens resource and the schema summary."""
    lines = [f"{view['name']} - {view['description']}".rstrip(" -")]
    for member, value in view.items():
        if member in ("name", "description"):
            continue
        shown = ", ".join(value) if isinstance(value, list) else value
        lines.append(f"  {member}: {shown}")
    return "\n".join(lines)


def _attribute_line(attribute: Mapping[str, Any]) -> str:
    mark = "!" if attribute.get("required") else ""
    derived = " (derived)" if attribute.get("derived") else ""
    return f"{attribute['name']}: {attribute['type']}{mark}{derived}"


def schema_text(view: Mapping[str, Any], *, detail: str, output: OutputSettings) -> str:
    full = detail == "full"
    limit = output.max_value_chars
    lines = [f"schema {view['domain']}@{view['branch']} rev {view['revision']}", "KINDS"]
    for kind in view["kinds"]:
        facts = [f"key {','.join(kind['key'])}" if kind["key"] else "key: given on create"]
        if kind.get("title"):
            facts.append(f"title {kind['title']}")
        if kind.get("traits"):
            facts.append(f"traits {','.join(kind['traits'])}")
        if kind.get("status"):
            facts.append(kind["status"])
        lines.append(
            f"{kind['name']} [{'; '.join(facts)}] - {value_text(kind['description'], limit)}"
        )
        if full:
            for attribute in kind["attributes"]:
                described = attribute.get("description")
                suffix = f" - {value_text(described, limit)}" if described else ""
                lines.append(f"  {_attribute_line(attribute)}{suffix}")
        else:
            lines.append("  " + " · ".join(_attribute_line(a) for a in kind["attributes"]))
        if kind["operations"]:
            lines.append(f"  ops: {', '.join(kind['operations'])}")
    lines.append("RELATIONS (from -> to; inverse name reads to -> from)")
    for relation in view["relations"]:
        attrs = relation.get("attributes")
        attr_text = f" {{{', '.join(_attribute_line(a) for a in attrs)}}}" if attrs else ""
        how = relation.get("backing")
        lines.append(
            f"{relation['name']}: {relation['from']} -> {relation['to']} "
            f"({relation['cardinality']}; inverse {relation['inverse']}"
            f"{f'; {how}' if how else ''}){attr_text} - "
            f"{value_text(relation['description'], limit)}"
        )
    lines.append("OPERATIONS (writes; run with lorebase_execute)")
    for operation in view["operations"]:
        params = ", ".join(_attribute_line(p) for p in operation["parameters"])
        review = f" [review: {operation['review']}]" if operation["review"] != "never" else ""
        lines.append(
            f"{operation['name']}({params}){review} - {value_text(operation['description'], limit)}"
        )
        if full:
            for parameter in operation["parameters"]:
                if parameter.get("description"):
                    lines.append(f"  {parameter['name']}: {parameter['description']}")
            if operation.get("review_when"):
                lines.append(f"  review when: {operation['review_when']}")
    lines.append(
        "built-in: per Kind ops above, link_/unlink_<relation> for explicit Relations, "
        + ", ".join(view["generic_operations"])
    )
    if view.get("lenses"):
        lines.append("LENSES (read presets; pass `lens` to lorebase_search or lorebase_context)")
        for lens in view["lenses"]:
            members = [
                f"{member} {', '.join(lens[member])}"
                for member in ("kinds", "traits", "relations")
                if lens.get(member)
            ]
            scope = f" [{'; '.join(members)}]" if members else ""
            lines.append(f"{lens['name']}{scope} - {value_text(lens['description'], limit)}")
    search = view["search"]
    lines.append(f"SEARCH modes {', '.join(search['modes'])}")
    if search["embedding"]:
        lines.append(
            f"  semantic and hybrid need a query_vector of {search['embedding']['dims']} "
            f"dimensions made by the embedding model {search['embedding']['model']}"
        )
    return "\n".join(lines)


# Records -------------------------------------------------------------------------------------


def record_view(record: m.RecordItem) -> View:
    """A Record as `{ref, title, values, ...}`."""
    view: View = {
        "ref": ref(record.kind, record.key),
        "title": record.title,
        "values": record.values,
    }
    if record.updated_revision:
        view["updated_revision"] = record.updated_revision
    if record.updated_at:
        view["updated_at"] = timestamp(record.updated_at)
    if record.links is not None:
        view["links"] = [
            {"relation": c.relation, "direction": c.direction, "count": c.count}
            for c in record.links
        ]
    if record.included:
        view["included"] = {
            relation: [{"ref": ref(r.kind, r.key), "title": r.title} for r in rows]
            for relation, rows in record.included.items()
        }
    return view


def record_text(view: Mapping[str, Any], revision: str | None, output: OutputSettings) -> str:
    served = f" rev {revision}" if revision else ""
    lines = [f"{titled(view['ref'], view.get('title'))}{served}"]
    lines.extend(
        f"{name}: {value_text(value, output.max_value_chars)}"
        for name, value in view["values"].items()
    )
    links = view.get("links")
    if links is not None:
        counted = [f"{c['relation']}={c['count']}" for c in links if c["count"]]
        lines.append(f"links: {', '.join(counted)}" if counted else "links: none")
    return "\n".join(lines)


def rows_text(items: Iterable[Mapping[str, Any]], output: OutputSettings) -> list[str]:
    """One line per Record view, with included neighbors indented below it."""
    lines: list[str] = []
    for item in items:
        head = titled(item["ref"], item.get("title"))
        values = values_text(item.get("values") or {}, output.max_value_chars)
        lines.append(f"{head} | {values}" if values else head)
        for relation, neighbors in (item.get("included") or {}).items():
            shown = ", ".join(titled(n["ref"], n.get("title")) for n in neighbors)
            lines.append(f"  {relation}: {shown or '-'}")
    return lines


def coverage_view(coverage: m.Coverage | None) -> View | None:
    """The coverage block (design 8.11) whole: it is small, and its notes are what an agent
    reads before concluding that nothing matches."""
    if coverage is None:
        return None
    return coverage.model_dump(mode="json", exclude_defaults=True)


def coverage_lines(view: Mapping[str, Any]) -> list[str]:
    """One line per coverage signal, never cut."""
    coverage = view.get("coverage") or {}
    lines = [f"coverage: {note}" for note in coverage.get("notes", [])]
    if coverage.get("omitted"):
        lines.append(f"coverage omitted: {coverage['omitted']}")
    return lines


def page_view(body: m.QueryResult, output: OutputSettings) -> View:
    """A page of Records (`/query`)."""
    items, omitted = capped([record_view(r) for r in body.items], output.max_items)
    view: View = {"revision": body.revision, "items": items}
    if omitted:
        view["omitted"] = omitted
    if body.next_cursor:
        view["next_cursor"] = body.next_cursor
    if body.aggregate is not None:
        view["aggregate"] = body.aggregate.model_dump(mode="json", exclude_unset=True)
    if (coverage := coverage_view(body.coverage)) is not None:
        view["coverage"] = coverage
    return view


def page_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    lines = [f"rev {view['revision']}: {len(view['items'])} rows"]
    aggregate = view.get("aggregate")
    if aggregate:
        for count in aggregate.get("counts") or []:
            group = (
                f" {value_text(count.get('value'), 60)}" if count.get("value") is not None else ""
            )
            lines.append(f"count {count['kind']}{group}: {count['count']}")
        if aggregate.get("truncated"):
            lines.append("aggregate truncated: too many groups")
    lines.extend(rows_text(view["items"], output))
    lines.extend(more_line(view.get("omitted", 0), "lower `limit`"))
    if view.get("next_cursor"):
        lines.append(f"next_cursor: {view['next_cursor']}")
    lines.extend(coverage_lines(view))
    return "\n".join(lines)


# Graph and search ----------------------------------------------------------------------------


def edge_view(edge: m.Edge) -> View:
    view: View = {
        "from": ref(edge.source.kind, edge.source.key),
        "relation": edge.relation,
        "to": ref(edge.to.kind, edge.to.key),
    }
    if edge.values:
        view["values"] = edge.values
    return view


def neighborhood_view(body: m.NeighborhoodResult, output: OutputSettings, *, values: bool) -> View:
    nodes = []
    for node in body.nodes:
        item = record_view(node)
        if not values:
            item.pop("values", None)
        item.pop("updated_revision", None)
        item.pop("updated_at", None)
        nodes.append(item)
    shown_nodes, nodes_omitted = capped(nodes, output.max_items)
    edges, edges_omitted = capped([edge_view(e) for e in body.edges], output.max_items)
    view: View = {
        "revision": body.revision,
        "nodes": shown_nodes,
        "edges": edges,
        "truncated": body.truncated,
    }
    if nodes_omitted or edges_omitted:
        view["omitted"] = {"nodes": nodes_omitted, "edges": edges_omitted}
    return view


def neighborhood_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    omitted = view.get("omitted") or {}
    lines = [
        f"rev {view['revision']}: {len(view['nodes'])} nodes, {len(view['edges'])} edges",
        "nodes:",
    ]
    lines.extend(rows_text(view["nodes"], output))
    lines.extend(more_line(omitted.get("nodes", 0)))
    lines.append("edges:")
    lines.extend(f"{e['from']} -{e['relation']}-> {e['to']}" for e in view["edges"])
    lines.extend(more_line(omitted.get("edges", 0)))
    if view["truncated"]:
        lines.append("truncated: Keeper caps reached; narrow `relations` or lower `depth`")
    return "\n".join(lines)


def search_view(body: m.SearchResult, output: OutputSettings) -> View:
    items, omitted = capped(
        [
            {
                "ref": ref(hit.kind, hit.key),
                "title": hit.title,
                "snippet": hit.snippet,
                "score": round(hit.score, 4),
            }
            for hit in body.items
        ],
        output.max_items,
    )
    view: View = {"revision": body.revision, "mode": body.mode, "items": items}
    if body.corrections:
        view["corrections"] = [
            {"word": c.word, "replacements": c.replacements} for c in body.corrections
        ]
    if body.truncated_candidates:
        view["truncated_candidates"] = True
    if body.semantic_truncated:
        view["truncated"] = True
    if omitted:
        view["omitted"] = omitted
    return view


def search_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    lines = [f"rev {view['revision']} ({view['mode']}): {len(view['items'])} hits"]
    for correction in view.get("corrections", []):
        lines.append(
            f"also searched {correction['word']} as {', '.join(correction['replacements'])}"
        )
    if view.get("truncated_candidates"):
        lines.append(
            "truncated: every term is very common, only a sample of the matches was ranked; "
            "add a rarer term or narrow `kinds`"
        )
    if view.get("truncated"):
        lines.append(
            "truncated: the vector search stopped before filling its list; "
            "narrow `kinds`, `traits` or `where`"
        )
    for index, hit in enumerate(view["items"], start=1):
        snippet = hit.get("snippet")
        tail = f" - {value_text(snippet, output.max_value_chars)}" if snippet else ""
        lines.append(f"{index}. {titled(hit['ref'], hit.get('title'))} ({hit['score']}){tail}")
    lines.extend(more_line(view.get("omitted", 0)))
    return "\n".join(lines)


def resolve_view(body: m.ResolveResult, output: OutputSettings) -> View:
    shown, omitted = capped(
        [
            {
                key: value
                for key, value in {
                    "target": c.target,
                    "ref": c.kind if c.attribute is None else f"{c.kind}.{c.attribute}",
                    "type": c.type,
                    "values": c.values,
                    "predicate": c.predicate,
                    "records": c.records,
                    "level": c.level,
                    "score": round(c.score, 4),
                    "matched": c.matched,
                }.items()
                if value is not None
            }
            for c in body.candidates
        ],
        output.max_items,
    )
    view: View = {"revision": body.revision, "candidates": shown}
    if body.unmatched:
        view["unmatched"] = body.unmatched
    if body.skipped:
        view["skipped"] = [{"level": s.level, "reason": s.reason} for s in body.skipped]
    if omitted:
        view["omitted"] = omitted
    if (coverage := coverage_view(body.coverage)) is not None:
        view["coverage"] = coverage
    return view


def resolve_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    lines = [f"rev {view['revision']}: {len(view['candidates'])} candidates"]
    for index, candidate in enumerate(view["candidates"], start=1):
        head = f"{index}. {candidate['target']} {candidate['ref']}"
        if candidate.get("type"):
            head += f" ({candidate['type']})"
        if candidate.get("values"):
            head += " = " + ", ".join(
                value_text(v, output.max_value_chars) for v in candidate["values"]
            )
        tail = f"{candidate['level']} {candidate['score']} for {candidate['matched']!r}"
        if candidate.get("records") is not None:
            tail += f", {candidate['records']} records"
        lines.append(f"{head} | {tail}")
        if candidate.get("predicate"):
            # Whole, never cut at max_value_chars: it is meant to be copied into a query.
            lines.append(f"   where: {as_json(candidate['predicate'])}")
    lines.extend(more_line(view.get("omitted", 0), "lower `limit` or name `kinds`"))
    if view.get("unmatched"):
        lines.append("unmatched: " + ", ".join(view["unmatched"]))
    for skipped in view.get("skipped", []):
        lines.append(f"skipped {skipped['level']}: {skipped['reason']}")
    lines.extend(coverage_lines(view))
    return "\n".join(lines)


# Explanation ----------------------------------------------------------------------------------


def explain_view(body: m.Explain, attributes: Sequence[str] | None, output: OutputSettings) -> View:
    """Keeper's explanation of a Record (`.../explain`), its lists capped; with `attributes`,
    only those attributes and no Links or Proposals."""
    record = body.record
    rows = list(body.attributes)
    if attributes:
        wanted = set(attributes)
        rows = [row for row in rows if row.get("name") in wanted]
    shown, omitted = capped(rows, output.max_items)
    view: View = {
        "revision": body.revision,
        "branch": body.branch,
        "ref": record["ref"],
        "title": record.get("title"),
        "updated_revision": record.get("updated_revision"),
        "history": body.history,
        "created": body.created,
        "attributes": shown,
    }
    if omitted:
        view["omitted"] = omitted
    if not attributes:
        links, more_links = capped(body.links, output.max_items)
        view["links"] = links
        view["links_omitted"] = more_links
        view["links_truncated"] = body.links_truncated
        view["proposals"] = list(body.proposals)
    return view


def _origin_text(entry: Mapping[str, Any]) -> str:
    if entry.get("revision") is None and entry.get("origin_type") is None:
        return "no recorded origin"
    message = entry.get("message")
    return (
        f"{entry.get('origin_type') or '?'} by {entry.get('actor') or '?'} at "
        f"{entry.get('at') or '?'} in {entry.get('revision') or '?'}"
        + (f": {message}" if message else "")
    )


def explain_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    history = view.get("history") or {}
    lines = [
        f"{titled(view['ref'], view.get('title'))} rev {view['revision']} on {view['branch']} "
        f"({history.get('changes', '?')} changes, last in {view.get('updated_revision')})"
    ]
    if created := view.get("created"):
        lines.append(f"created <- {_origin_text(created)}")
    for entry in view["attributes"]:
        value = value_text(entry.get("value"), output.max_value_chars)
        lines.append(f"{entry['name']} = {value} <- {_origin_text(entry)}")
    lines.extend(more_line(view.get("omitted", 0), "name `attributes`"))
    links = view.get("links") or []
    if links:
        lines.append("LINKS:")
    for link in links:
        values = link.get("values")
        extra = f" ({values_text(values, output.max_value_chars)})" if values else ""
        lines.append(
            f"{link['relation']} -> {titled(link['ref'], link.get('title'))}{extra} "
            f"<- {_origin_text(link)}"
        )
    lines.extend(more_line(view.get("links_omitted", 0)))
    if view.get("links_truncated"):
        lines.append("... more Links than Keeper lists (agents.explain.max_links)")
    proposals = view.get("proposals") or []
    if proposals:
        lines.append("PROPOSALS:")
    for proposal in proposals:
        merged = proposal.get("merged_revision")
        lines.append(
            f'{proposal["id"]} "{proposal.get("title", "")}" {proposal.get("status", "?")}'
            + (f" in {merged}" if merged else "")
        )
    return "\n".join(lines)


# Record history and lineage -------------------------------------------------------------------


def _present(**members: Any) -> View:
    """The members that carry a value."""
    return {k: v for k, v in members.items() if v is not None}


def _change_entry(change: m.RecordChange, *, values: bool) -> View:
    attributes = []
    for attribute in change.attributes:
        entry = _present(name=attribute.name)
        if values:
            entry.update(before=attribute.before, after=attribute.after)
        if attribute.provenance:
            entry["origin_type"] = attribute.provenance.origin_type
        attributes.append(entry)
    return _present(
        revision=change.revision,
        at=timestamp(change.at),
        author=change.author,
        on_behalf_of=change.on_behalf_of,
        origin=change.origin.get("type"),
        change=change.change,
        message=change.message,
        attributes=attributes,
    )


def history_view(body: m.RecordHistory, output: OutputSettings, *, values: bool) -> View:
    """The changes of a Record, newest first (`.../history`), capped."""
    entries = [_change_entry(c, values=values) for c in body.items]
    shown, omitted = capped(entries, output.max_items)
    view: View = {
        "revision": body.revision,
        "branch": body.branch,
        "ref": ref(body.kind, body.key),
        "items": shown,
    }
    if omitted:
        view["omitted"] = omitted
    if body.next_cursor:
        view["next_cursor"] = body.next_cursor
    return view


def history_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    lines = [
        f"{view['ref']} rev {view['revision']} on {view['branch']}: {len(view['items'])} changes"
    ]
    for item in view["items"]:
        origin = f" [{item['origin']}]" if item.get("origin") else ""
        lines.append(
            f"{item['revision']} {item['at']} {item['author']}{origin} {item['change']}: "
            f"{value_text(item['message'], output.max_value_chars)}"
        )
        for attribute in item["attributes"]:
            if "after" in attribute or "before" in attribute:
                before = value_text(attribute.get("before"), output.max_value_chars)
                after = value_text(attribute.get("after"), output.max_value_chars)
                lines.append(f"  {attribute['name']}: {before} -> {after}")
            else:
                lines.append(f"  {attribute['name']}")
    lines.extend(more_line(view.get("omitted", 0), "lower `limit`"))
    if view.get("next_cursor"):
        lines.append(f"next_cursor: {view['next_cursor']}")
    return "\n".join(lines)


def lineage_view(body: m.RecordLineage, output: OutputSettings) -> View:
    """Where the current values of a Record came from (`.../lineage`), capped."""
    rows: list[View] = []
    for item in body.attributes:
        provenance = item.provenance
        rows.append(
            _present(
                name=item.name,
                value=item.value,
                origin_type=provenance.origin_type if provenance else None,
                actor=provenance.actor if provenance else None,
                at=timestamp(provenance.at) if provenance else None,
                revision=item.revision,
                author=item.author,
                message=item.message,
                source=item.source,
                operation=item.operation,
                extraction=item.extraction.model_dump(mode="json") if item.extraction else None,
                proposal=item.proposal.model_dump(mode="json") if item.proposal else None,
            )
        )
    shown, omitted = capped(rows, output.max_items)
    view: View = {
        "revision": body.revision,
        "branch": body.branch,
        "ref": ref(body.kind, body.key),
        "changes": body.changes,
        "created": _change_entry(body.created, values=False) if body.created else None,
        "attributes": shown,
        "sources": body.sources,
        "proposals": [p.model_dump(mode="json") for p in body.proposals],
    }
    if omitted:
        view["omitted"] = omitted
    return view


def lineage_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    lines = [
        f"{view['ref']} rev {view['revision']} on {view['branch']} ({view['changes']} changes)"
    ]
    if created := view.get("created"):
        lines.append(f"created <- {created['author']} at {created['at']} in {created['revision']}")
    for entry in view["attributes"]:
        value = value_text(entry.get("value"), output.max_value_chars)
        origin = entry.get("origin_type") or "no recorded origin"
        detail = [
            f"{label} {entry[key]}"
            for label, key in (
                ("by", "actor"),
                ("at", "at"),
                ("in", "revision"),
                ("source", "source"),
                ("operation", "operation"),
            )
            if entry.get(key)
        ]
        lines.append(f"{entry['name']} = {value} <- {origin} {' '.join(detail)}".rstrip())
        if evidence := entry.get("extraction"):
            lines.append(
                f"  extracted from document {evidence['document_id']} "
                f"[{evidence['span_start']}:{evidence['span_end']}] by {evidence['model_id']}"
            )
    lines.extend(more_line(view.get("omitted", 0)))
    if view["sources"]:
        lines.append("SOURCES: " + ", ".join(view["sources"]))
    for proposal in view["proposals"]:
        merged = proposal.get("merged_revision")
        lines.append(
            f'PROPOSAL {proposal["id"]} "{proposal["title"]}" {proposal["status"]}'
            + (f" in {merged}" if merged else "")
        )
    return "\n".join(lines)


# History, diff, branches ---------------------------------------------------------------------


def log_entry(item: m.Revision) -> View:
    return {
        "revision": item.id,
        "at": timestamp(item.created_at),
        "author": item.author,
        "origin": item.origin.get("type"),
        "message": item.message,
        "stats": item.stats,
    }


def changes_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    since = view.get("since")
    scope = f" since {since}" if since else ""
    lines = [f"{view['branch']} head {view['head']}: {len(view['items'])} Revisions{scope}"]
    for item in view["items"]:
        stats = values_text(item["stats"], 40, ", ") if item["stats"] else ""
        lines.append(
            f"{item['revision']} {item['at']} {item['author']} [{item['origin']}] "
            f"{value_text(item['message'], output.max_value_chars)}"
            + (f" ({stats})" if stats else "")
        )
    if since and not view.get("since_reached"):
        lines.append(
            f"since not reached: {since} is older than the entries read or not on this branch"
        )
    if view.get("next_cursor"):
        lines.append(f"next_cursor: {view['next_cursor']}")
    return "\n".join(lines)


def _change_subject(change: m.Change) -> str:
    if change.kind and change.key:
        return ref(change.kind, change.key)
    if change.relation:
        return f"{change.relation} {change.from_key} -> {change.to_key}"
    label = change.api_name or change.key or change.partition
    return f"{change.element_type or change.element} {label}"


def diff_view(body: m.ChangesPage, output: OutputSettings) -> View:
    changes = []
    for change in body.items:
        item: View = {"change": change.change, "subject": _change_subject(change)}
        if change.changes:
            item["values"] = [{"name": c.name, "old": c.old, "new": c.new} for c in change.changes]
        changes.append(item)
    shown, omitted = capped(changes, output.max_items)
    view: View = {
        "from": body.from_revision,
        "to": body.to_revision,
        "total": body.total,
        "summary": [
            {
                "name": s.name,
                "element": s.element,
                "added": s.added,
                "modified": s.modified,
                "removed": s.removed,
            }
            for s in body.summary
        ],
        "changes": shown,
    }
    if omitted:
        view["omitted"] = omitted
    if body.next_cursor:
        view["next_cursor"] = body.next_cursor
    return view


_SYMBOL = {"added": "+", "removed": "-", "modified": "~"}


def diff_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    limit = output.max_value_chars
    lines = [f"diff {view['from']}..{view['to']}: {view['total']} changes"]
    summary = [
        f"{s['name']} +{s['added']} ~{s['modified']} -{s['removed']}" for s in view["summary"]
    ]
    if summary:
        lines.append("summary: " + "; ".join(summary))
    for change in view["changes"]:
        lines.append(f"{_SYMBOL.get(change['change'], '?')} {change['subject']}")
        lines.extend(
            f"  {v['name']}: {value_text(v['old'], limit)} -> {value_text(v['new'], limit)}"
            for v in change.get("values") or []
        )
    lines.extend(more_line(view.get("omitted", 0), "lower `limit`"))
    if view.get("next_cursor"):
        lines.append(f"next_cursor: {view['next_cursor']}")
    return "\n".join(lines)


def branches_view(body: m.Page[m.Branch], output: OutputSettings) -> View:
    items, omitted = capped(
        [
            {"name": b.name, "head": b.head, "default": b.default, "protected": b.protected}
            for b in body.items
        ],
        output.max_items,
    )
    view: View = {"items": items}
    if omitted:
        view["omitted"] = omitted
    if body.next_cursor:
        view["next_cursor"] = body.next_cursor
    return view


def branches_text(view: Mapping[str, Any]) -> str:
    lines = []
    for branch in view["items"]:
        flags = [f for f in ("default", "protected") if branch[f]]
        tag = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"{branch['name']}{tag} head {branch['head']}")
    lines.extend(more_line(view.get("omitted", 0)))
    if view.get("next_cursor"):
        lines.append(f"next_cursor: {view['next_cursor']}")
    return "\n".join(lines) or "no branches visible to this agent"


# Operations ----------------------------------------------------------------------------------


def proposal_write_view(body: m.AgentWrite, output: OutputSettings) -> View:
    """An Operation that needed review and was proposed on a task branch (Keeper's 202)."""
    steps, _ = capped(body.next, output.max_items)
    warnings, _ = capped(body.warnings, output.max_issues)
    proposal, approval = body.proposal, body.approval
    return {
        "status": body.status,
        "operation": body.operation,
        "branch": body.branch,
        "target": body.target,
        "revision": body.revision,
        "merged_revision": body.merged_revision,
        "edit_count": body.edit_count,
        "proposal": {
            "id": proposal.id,
            "status": proposal.status,
            "title": proposal.title,
            "url": proposal.url,
        },
        "approvals": {
            "approvals": approval.approvals,
            "required": approval.required,
            "satisfied": approval.satisfied,
        },
        "open_conflicts": body.attempt.open_conflicts if body.attempt else 0,
        "merge": body.merge.model_dump(mode="json"),
        "warnings": warnings,
        "replayed": body.replayed,
        "next": [step.model_dump(mode="json") for step in steps],
    }


def execution_view(body: m.Execution | m.AgentWrite, output: OutputSettings) -> View:
    if isinstance(body, m.AgentWrite):
        return proposal_write_view(body, output)
    edits, _ = capped(body.edits, output.max_items)
    violations, _ = capped(body.violations, output.max_issues)
    warnings, _ = capped(body.warnings, output.max_issues)
    return {
        "status": body.status,
        "operation": body.operation,
        "branch": body.branch,
        "head": body.head,
        "revision": body.revision,
        "review_required": body.review_required,
        "edit_count": body.edit_count,
        "edits": edits,
        "edits_omitted": max(0, body.edit_count - len(edits)),
        "violation_count": body.violation_count,
        "violations": violations,
        "warnings": warnings,
        "replayed": body.replayed,
    }


def _finding(finding: Mapping[str, Any]) -> str:
    where = "/".join(str(p) for p in finding.get("path") or [])
    return f"- {where or '<operation>'}: {finding.get('message', '')} [{finding.get('code')}]"


def proposal_write_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    proposal = view["proposal"]
    approvals = view["approvals"]
    lines = [
        f"{view['status']} {view['operation']} on {view['branch']} -> {view['target']}: "
        f"Proposal {proposal['id']} ({proposal['status']}), {view['edit_count']} edits"
    ]
    if view.get("revision"):
        lines.append(f"revision {view['revision']}")
    if view.get("merged_revision"):
        lines.append(f"merged: {view['target']} is now at {view['merged_revision']}")
    lines.append(f"approvals {approvals['approvals']} of {approvals['required']} required")
    if view["open_conflicts"]:
        lines.append(f"open conflicts: {view['open_conflicts']}")
    merge = view.get("merge") or {}
    if merge.get("requested") and not merge.get("performed"):
        reason = ", ".join(merge.get("blockers") or []) or (
            "not allowed for this role" if not merge.get("allowed") else "conflicts"
        )
        lines.append(f"auto_merge requested, not performed: {reason}")
    if view["warnings"]:
        lines.append("warnings:")
        lines.extend(_finding(w) for w in view["warnings"])
    if view["replayed"]:
        lines.append("replayed: same idempotency_key and parameters; original result returned")
    for step in view["next"]:
        call = f" [{step['method']} {step['href']}]" if step.get("href") else ""
        detail = value_text(step["detail"], output.max_value_chars)
        lines.append(f"next: {step['action']}: {detail}{call}")
    return "\n".join(lines)


def execution_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    if "proposal" in view:
        return proposal_write_text(view, output)
    limit = output.max_value_chars * 2
    status = view["status"]
    lines = [
        f"{status} {view['operation']} on {view['branch']} (head {view['head']}): "
        f"{view['edit_count']} edits, {view['violation_count']} violations"
    ]
    if view.get("revision"):
        lines.append(f"revision {view['revision']}")
    if view["edits"]:
        lines.append("edits:")
        lines.extend(f"- {value_text(edit, limit)}" for edit in view["edits"])
        lines.extend(more_line(view["edits_omitted"]))
    if view["violations"]:
        lines.append("violations:")
        lines.extend(_finding(v) for v in view["violations"])
    if view["warnings"]:
        lines.append("warnings:")
        lines.extend(_finding(w) for w in view["warnings"])
    if view["replayed"]:
        lines.append("replayed: same idempotency_key and parameters; original result returned")
    if status == "dry_run":
        if view["violations"]:
            lines.append("next: fix the violations; nothing was written")
        elif view["review_required"]:
            lines.append(
                "next: committing on this branch needs review: run it on a work branch, then "
                "open a Proposal"
            )
        else:
            lines.append(
                f"next: call again with dry_run=false and expected_head={view['head']} to commit"
            )
    return "\n".join(lines)
