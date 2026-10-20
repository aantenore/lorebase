"""Keeper responses turned into compact views (the `json` format) and their text form.

Views keep api names and `Kind/key` refs, drop internal ids, and always carry the Revision a
response was served from. Lists are capped at `output.max_items` with an explicit count of what
was left out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lorebase_herald.render import (
    capped,
    more_line,
    ref,
    titled,
    value_text,
    values_text,
)
from lorebase_herald.settings import OutputSettings

GENERIC_OPERATIONS = ("import_records(kind, rows)", "import_links(relation, rows)")
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


def schema_view(document: Mapping[str, Any], *, kinds: Sequence[str] | None, detail: str) -> View:
    """Kinds, Relations and Operations of a schema document (`GET .../schema`)."""
    content: Mapping[str, Any] = document.get("schema") or {}
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
    return {
        "domain": document.get("domain"),
        "branch": document.get("branch"),
        "revision": document.get("revision"),
        "kinds": kind_views,
        "relations": relation_views,
        "operations": operation_views,
        "generic_operations": list(GENERIC_OPERATIONS),
    }


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
    return "\n".join(lines)


def involved_schema_lines(view: Mapping[str, Any]) -> list[str]:
    """One line per Kind and Relation, for context packs."""
    lines = [
        f"{k['name']}: " + " · ".join(_attribute_line(a) for a in k["attributes"])
        for k in view["kinds"]
    ]
    lines.extend(
        f"{r['name']}: {r['from']} -> {r['to']} ({r['cardinality']}; inverse {r['inverse']})"
        for r in view["relations"]
    )
    return lines


# Records -------------------------------------------------------------------------------------


def record_view(record: Mapping[str, Any]) -> View:
    """A Record as `{ref, title, values, ...}`."""
    view: View = {
        "ref": ref(record["kind"], record["key"]),
        "title": record.get("title"),
        "values": record.get("values") or {},
    }
    if record.get("updated_revision"):
        view["updated_revision"] = record["updated_revision"]
    if record.get("updated_at"):
        view["updated_at"] = record["updated_at"]
    if record.get("links") is not None:
        view["links"] = [
            {"relation": c["relation"], "direction": c["direction"], "count": c["count"]}
            for c in record["links"]
        ]
    included = record.get("included")
    if included:
        view["included"] = {
            relation: [{"ref": ref(r["kind"], r["key"]), "title": r.get("title")} for r in rows]
            for relation, rows in included.items()
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


def page_view(body: Mapping[str, Any], output: OutputSettings) -> View:
    """A page of Records (`/query`)."""
    items, omitted = capped([record_view(r) for r in body.get("items") or []], output.max_items)
    view: View = {"revision": body.get("revision"), "items": items}
    if omitted:
        view["omitted"] = omitted
    if body.get("next_cursor"):
        view["next_cursor"] = body["next_cursor"]
    if body.get("aggregate"):
        view["aggregate"] = body["aggregate"]
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
    return "\n".join(lines)


# Graph and search ----------------------------------------------------------------------------


def edge_view(edge: Mapping[str, Any]) -> View:
    source, target = edge["from"], edge["to"]
    view: View = {
        "from": ref(source["kind"], source["key"]),
        "relation": edge["relation"],
        "to": ref(target["kind"], target["key"]),
    }
    if edge.get("values"):
        view["values"] = edge["values"]
    return view


def neighborhood_view(body: Mapping[str, Any], output: OutputSettings, *, values: bool) -> View:
    nodes = []
    for node in body.get("nodes") or []:
        item = record_view(node)
        if not values:
            item.pop("values", None)
        item.pop("updated_revision", None)
        item.pop("updated_at", None)
        nodes.append(item)
    shown_nodes, nodes_omitted = capped(nodes, output.max_items)
    edges, edges_omitted = capped([edge_view(e) for e in body.get("edges") or []], output.max_items)
    view: View = {
        "revision": body.get("revision"),
        "nodes": shown_nodes,
        "edges": edges,
        "truncated": bool(body.get("truncated")),
    }
    if nodes_omitted or edges_omitted:
        view["omitted"] = {"nodes": nodes_omitted, "edges": edges_omitted}
    return view


def edge_line(edge: Mapping[str, Any], titles: Mapping[str, str | None], max_chars: int) -> str:
    values = edge.get("values")
    suffix = f" {{{values_text(values, max_chars)}}}" if values else ""
    return (
        f"{titled(edge['from'], titles.get(edge['from']))} -{edge['relation']}-> "
        f"{titled(edge['to'], titles.get(edge['to']))}{suffix}"
    )


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


def search_view(body: Mapping[str, Any], output: OutputSettings) -> View:
    items, omitted = capped(
        [
            {
                "ref": ref(hit["kind"], hit["key"]),
                "title": hit.get("title"),
                "snippet": hit.get("snippet"),
                "score": round(float(hit.get("score") or 0), 4),
            }
            for hit in body.get("items") or []
        ],
        output.max_items,
    )
    view: View = {"revision": body.get("revision"), "mode": body.get("mode"), "items": items}
    if omitted:
        view["omitted"] = omitted
    return view


def search_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    lines = [f"rev {view['revision']} ({view['mode']}): {len(view['items'])} hits"]
    for index, hit in enumerate(view["items"], start=1):
        snippet = hit.get("snippet")
        tail = f" - {value_text(snippet, output.max_value_chars)}" if snippet else ""
        lines.append(f"{index}. {titled(hit['ref'], hit.get('title'))} ({hit['score']}){tail}")
    lines.extend(more_line(view.get("omitted", 0)))
    return "\n".join(lines)


# Lineage ------------------------------------------------------------------------------------


def lineage_view(
    body: Mapping[str, Any], attributes: Sequence[str] | None, output: OutputSettings
) -> View:
    record = body["record"]
    values: Mapping[str, Any] = record.get("values") or {}
    lineage: Mapping[str, Any] = record.get("lineage") or {}
    wanted = list(attributes) if attributes else list(values)
    entries = []
    for name in wanted:
        entry = lineage.get(name) or {}
        entries.append(
            {
                "attribute": name,
                "value": values.get(name),
                "origin_type": entry.get("origin_type"),
                "actor": entry.get("actor"),
                "at": entry.get("at"),
                "provenance": entry.get("provenance_id"),
            }
        )
    shown, omitted = capped(entries, output.max_items)
    view: View = {
        "revision": body.get("revision"),
        "ref": ref(record["kind"], record["key"]),
        "title": record.get("title"),
        "updated_revision": record.get("updated_revision"),
        "lineage": shown,
    }
    if omitted:
        view["omitted"] = omitted
    return view


def lineage_text(view: Mapping[str, Any], output: OutputSettings) -> str:
    lines = [
        f"{titled(view['ref'], view.get('title'))} rev {view['revision']} "
        f"(last changed in {view.get('updated_revision')})"
    ]
    for entry in view["lineage"]:
        value = value_text(entry["value"], output.max_value_chars)
        if entry["provenance"] is None:
            lines.append(f"{entry['attribute']} = {value} <- no recorded origin")
            continue
        lines.append(
            f"{entry['attribute']} = {value} <- {entry['origin_type'] or '?'} by "
            f"{entry['actor'] or '?'} at {entry['at'] or '?'} ({entry['provenance']})"
        )
    lines.extend(more_line(view.get("omitted", 0), "name `attributes`"))
    return "\n".join(lines)


def explain_view(
    body: Mapping[str, Any], attributes: Sequence[str] | None, output: OutputSettings
) -> View:
    """Keeper's explanation of a Record (`.../explain`), its lists capped; with `attributes`,
    only those attributes and no Links or Proposals."""
    record = body["record"]
    rows: list[Mapping[str, Any]] = list(body.get("attributes") or [])
    if attributes:
        wanted = set(attributes)
        rows = [row for row in rows if row.get("name") in wanted]
    shown, omitted = capped(rows, output.max_items)
    view: View = {
        "revision": body.get("revision"),
        "branch": body.get("branch"),
        "ref": record["ref"],
        "title": record.get("title"),
        "updated_revision": record.get("updated_revision"),
        "history": body.get("history"),
        "created": body.get("created"),
        "attributes": shown,
    }
    if omitted:
        view["omitted"] = omitted
    if not attributes:
        links, more_links = capped(body.get("links") or [], output.max_items)
        view["links"] = links
        view["links_omitted"] = more_links
        view["links_truncated"] = bool(body.get("links_truncated"))
        view["proposals"] = list(body.get("proposals") or [])
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


# History, diff, branches ---------------------------------------------------------------------


def log_entry(item: Mapping[str, Any]) -> View:
    origin = item.get("origin") or {}
    return {
        "revision": item["id"],
        "at": item.get("created_at"),
        "author": item.get("author"),
        "origin": origin.get("type"),
        "message": item.get("message") or "",
        "stats": item.get("stats") or {},
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


def _change_subject(change: Mapping[str, Any]) -> str:
    if change.get("kind") and change.get("key"):
        return ref(change["kind"], change["key"])
    if change.get("relation"):
        return f"{change['relation']} {change.get('from_key')} -> {change.get('to_key')}"
    label = change.get("api_name") or change.get("key") or change.get("partition")
    return f"{change.get('element_type') or change.get('element')} {label}"


def diff_view(body: Mapping[str, Any], output: OutputSettings) -> View:
    changes = []
    for change in body.get("items") or []:
        item: View = {"change": change["change"], "subject": _change_subject(change)}
        if change.get("changes"):
            item["values"] = [
                {"name": c["name"], "old": c.get("old"), "new": c.get("new")}
                for c in change["changes"]
            ]
        changes.append(item)
    shown, omitted = capped(changes, output.max_items)
    view: View = {
        "from": body.get("from_revision"),
        "to": body.get("to_revision"),
        "total": body.get("total"),
        "summary": [
            {
                "name": s["name"],
                "element": s["element"],
                "added": s["added"],
                "modified": s["modified"],
                "removed": s["removed"],
            }
            for s in body.get("summary") or []
        ],
        "changes": shown,
    }
    if omitted:
        view["omitted"] = omitted
    if body.get("next_cursor"):
        view["next_cursor"] = body["next_cursor"]
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


def branches_view(body: Mapping[str, Any], output: OutputSettings) -> View:
    items, omitted = capped(
        [
            {
                "name": b["name"],
                "head": b["head"],
                "default": b.get("default", False),
                "protected": b.get("protected", False),
            }
            for b in body.get("items") or []
        ],
        output.max_items,
    )
    view: View = {"items": items}
    if omitted:
        view["omitted"] = omitted
    if body.get("next_cursor"):
        view["next_cursor"] = body["next_cursor"]
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


def execution_view(body: Mapping[str, Any], output: OutputSettings) -> View:
    edits, _ = capped(body.get("edits") or [], output.max_items)
    edit_count = int(body.get("edit_count", len(body.get("edits") or [])))
    violations, _ = capped(body.get("violations") or [], output.max_issues)
    warnings, _ = capped(body.get("warnings") or [], output.max_issues)
    return {
        "status": body.get("status"),
        "operation": body.get("operation"),
        "branch": body.get("branch"),
        "head": body.get("head"),
        "revision": body.get("revision"),
        "review_required": bool(body.get("review_required")),
        "edit_count": edit_count,
        "edits": edits,
        "edits_omitted": max(0, edit_count - len(edits)),
        "violation_count": body.get("violation_count", len(violations)),
        "violations": violations,
        "warnings": warnings,
        "replayed": bool(body.get("replayed")),
    }


def _finding(finding: Mapping[str, Any]) -> str:
    where = "/".join(str(p) for p in finding.get("path") or [])
    return f"- {where or '<operation>'}: {finding.get('message', '')} [{finding.get('code')}]"


def execution_text(view: Mapping[str, Any], output: OutputSettings) -> str:
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
