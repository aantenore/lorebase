"""What a trace step keeps of a request and its response: pure functions over JSON.

- `query_of`: the query as structured data. The request body of a POST read, or the query
  parameters of a GET read. Values compared by filters (`attr` and `derived` predicates) are
  replaced by their type, except on fields the read found not sensitive (`plain`, with
  `capture_filter_values: non_sensitive`), where scalars and lists of scalars are kept, cut
  to `max_text_chars` and `max_filter_value_items`; `capture_literals` keeps them all. Search
  text (`q`, `search`, `match`) is kept unless `capture_search_text` is off; query vectors
  are replaced by their dimension and cursors dropped. Long strings are cut.
- `result_of`: counts of the lists of a response, plus the search mode and corrections, the
  context pack budget and truncation flags. No values.
- `refs_of`: the Records (Kind and key) and Links (Relation and both ends) a response
  touched. Members named `values` are never walked, so values of Records are never read
  even when they hold references.
- `proposal_of`: the Proposal a write opened or acted on.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from lorebase_keeper.tracing.config import TraceSettings

TEXT_MEMBERS = frozenset({"q", "search", "match"})
SKIPPED_PARAMS = frozenset({"cursor"})
COMPARED_FIELDS = ("attr", "derived")
MAX_LIST_ITEMS = 100
MAX_CORRECTIONS = 20
MAX_DEPTH = 32

RecordRef = tuple[str, str]
LinkRef = tuple[str, str, str, str, str]


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    return "object"


def _redacted(value: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"redacted": _type_of(value)}
    if isinstance(value, list):
        out["count"] = len(value)
    return out


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, bool | int | float | str)


class _Redactor:
    def __init__(self, settings: TraceSettings, plain: Callable[[str], bool] | None = None) -> None:
        self._settings = settings
        self._plain = plain if settings.capture_filter_values == "non_sensitive" else None

    def _plain_field(self, comparison: Mapping[str, Any]) -> bool:
        """Whether a predicate compares a field the read reported as not sensitive (every
        field it names, should it name both `attr` and `derived`)."""
        plain = self._plain
        fields = [comparison[name] for name in COMPARED_FIELDS if name in comparison]
        return (
            plain is not None
            and bool(fields)
            and all(isinstance(field, str) and plain(field) for field in fields)
        )

    def literal(self, value: Any) -> Any:
        """A compared value as kept: a scalar (strings cut) or a list of scalars (cut to
        `max_filter_value_items`); anything else by its type."""
        if isinstance(value, str):
            return self.text(value)
        if _scalar(value):
            return value
        if isinstance(value, list) and all(_scalar(item) for item in value):
            limit = self._settings.max_filter_value_items
            items: list[Any] = [
                self.text(item) if isinstance(item, str) else item for item in value[:limit]
            ]
            if len(value) > limit:
                items.append({"truncated": len(value) - limit})
            return items
        return _redacted(value)

    def text(self, value: str) -> str:
        limit = self._settings.max_text_chars
        return value if len(value) <= limit else value[:limit] + "…"

    def node(self, value: Any, depth: int = 0) -> Any:
        if depth > MAX_DEPTH:
            return {"truncated": True}
        if isinstance(value, Mapping):
            return self.mapping(value, depth)
        if isinstance(value, list):
            items = [self.node(item, depth + 1) for item in value[:MAX_LIST_ITEMS]]
            if len(value) > MAX_LIST_ITEMS:
                items.append({"truncated": len(value) - MAX_LIST_ITEMS})
            return items
        if isinstance(value, str):
            return self.text(value)
        return value

    def mapping(self, value: Mapping[str, Any], depth: int) -> dict[str, Any]:
        comparison = any(name in value for name in COMPARED_FIELDS) and "value" in value
        out: dict[str, Any] = {}
        for name, member in value.items():
            if name == "query_vector" and isinstance(member, list):
                out[name] = {"dims": len(member)}
            elif name in SKIPPED_PARAMS:
                continue
            elif name == "value" and comparison:
                if self._settings.capture_literals:
                    out[name] = self.node(member, depth + 1)
                elif self._plain_field(value):
                    out[name] = self.literal(member)
                else:
                    out[name] = _redacted(member)
            elif name in TEXT_MEMBERS and isinstance(member, str):
                out[name] = (
                    self.text(member)
                    if self._settings.capture_search_text
                    else {"redacted": "text", "length": len(member)}
                )
            else:
                out[name] = self.node(member, depth + 1)
        return out


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def query_of(
    settings: TraceSettings,
    *,
    params: Iterable[tuple[str, str]],
    body: bytes | None,
    body_complete: bool,
    plain: Callable[[str], bool] | None = None,
) -> dict[str, Any] | None:
    """The recorded query of a read: `{"body": ...}` and/or `{"params": ...}`. `plain` tells
    the fields whose compared values may be kept (see the module doc); None: none."""
    redactor = _Redactor(settings, plain)
    out: dict[str, Any] = {}
    collected: dict[str, Any] = {}
    for name, value in params:
        if name in SKIPPED_PARAMS:
            continue
        if name == "where":
            parsed = _parse(value.encode())
            collected[name] = (
                redactor.node(parsed) if parsed is not None else {"redacted": "string"}
            )
        elif name in TEXT_MEMBERS:
            collected[name] = redactor.mapping({name: value}, 0)[name]
        else:
            collected[name] = redactor.text(value)
    if collected:
        out["params"] = collected
    if body:
        if not body_complete:
            out["body"] = {"truncated": True, "bytes": len(body)}
        else:
            parsed = _parse(body)
            if isinstance(parsed, Mapping):
                out["body"] = redactor.mapping(parsed, 0)
    if not out:
        return None
    if len(json.dumps(out)) > settings.max_query_bytes:
        return {"truncated": True}
    return out


def branch_of(query: Mapping[str, Any] | None) -> str | None:
    """The branch a recorded query names, if any."""
    if not query:
        return None
    for part in ("body", "params"):
        section = query.get(part)
        if isinstance(section, Mapping) and isinstance(section.get("branch"), str):
            return str(section["branch"])
    return None


def result_of(document: Any, settings: TraceSettings) -> dict[str, Any] | None:
    """Counts and shape of a JSON response; never values. Search corrections name the query
    words, so they are kept only with `capture_search_text`."""
    if not isinstance(document, Mapping):
        return None
    counts: dict[str, int] = {}
    for name, member in document.items():
        if name == "corrections":
            continue
        if isinstance(member, list):
            counts[name] = len(member)
        elif (
            isinstance(member, Mapping)
            and member
            and all(isinstance(v, list) for v in member.values())
        ):
            counts[name] = sum(len(v) for v in member.values())
    hops = document.get("hops")
    if isinstance(hops, list):
        counts["hop_records"] = sum(
            len(h.get("records") or ()) for h in hops if isinstance(h, Mapping)
        )
    out: dict[str, Any] = {"counts": counts}
    if isinstance(document.get("mode"), str):
        out["mode"] = document["mode"]
    corrections = document.get("corrections")
    if isinstance(corrections, list) and corrections and settings.capture_search_text:
        out["corrections"] = [
            {"word": c.get("word"), "replacements": list(c.get("replacements") or ())[:10]}
            for c in corrections[:MAX_CORRECTIONS]
            if isinstance(c, Mapping)
        ]
    budget = document.get("budget")
    if isinstance(budget, Mapping):
        out["budget"] = {
            name: budget.get(name)
            for name in ("limit_tokens", "used_tokens", "over_budget", "capped")
            if name in budget
        }
    if isinstance(document.get("truncated"), bool):
        out["truncated"] = document["truncated"]
    if "next_cursor" in document:
        out["has_more"] = document.get("next_cursor") is not None
    aggregate = document.get("aggregate")
    if isinstance(aggregate, Mapping) and isinstance(aggregate.get("counts"), list):
        counts["aggregate_groups"] = len(aggregate["counts"])
    return out


def _ref(value: Any) -> RecordRef | None:
    if isinstance(value, Mapping):
        kind, key = value.get("kind"), value.get("key")
        if isinstance(kind, str) and isinstance(key, str):
            return kind, key
        return None
    if isinstance(value, str) and "/" in value:
        kind, _, key = value.partition("/")
        if kind and key:
            return kind, key
    return None


class _Refs:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.records: dict[RecordRef, None] = {}
        self.links: dict[LinkRef, None] = {}
        self.truncated = False

    def record(self, ref: RecordRef | None) -> None:
        if ref is None or ref in self.records:
            return
        if len(self.records) + len(self.links) >= self.limit:
            self.truncated = True
            return
        self.records[ref] = None

    def link(self, relation: str, source: RecordRef | None, target: RecordRef | None) -> None:
        if source is None or target is None:
            return
        self.record(source)
        self.record(target)
        key = (relation, *source, *target)
        if key in self.links:
            return
        if len(self.records) + len(self.links) >= self.limit:
            self.truncated = True
            return
        self.links[key] = None

    def walk(self, node: Any, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            return
        if isinstance(node, list):
            for item in node:
                self.walk(item, depth + 1)
            return
        if not isinstance(node, Mapping):
            return
        self.record(_ref(node))
        ref = node.get("ref")
        if isinstance(ref, str):
            self.record(_ref(ref))
        relation = node.get("relation")
        if isinstance(relation, str):
            self.link(relation, _ref(node.get("from")), _ref(node.get("to")))
        for name, member in node.items():
            if name == "values":
                continue
            if name == "links" and isinstance(member, Mapping):
                self._pack_links(member)
                continue
            if isinstance(member, Mapping | list):
                self.walk(member, depth + 1)

    def _pack_links(self, links: Mapping[str, Any]) -> None:
        """Context pack Links: `{relation: [{"from": "Kind/key", "to": "Kind/key"}]}`."""
        for relation, items in links.items():
            for item in items if isinstance(items, list) else ():
                if isinstance(item, Mapping):
                    self.link(relation, _ref(item.get("from")), _ref(item.get("to")))


def refs_of(document: Any, limit: int) -> dict[str, Any] | None:
    """Records and Links a JSON response touched, at most `limit` in all."""
    refs = _Refs(limit)
    refs.walk(document)
    if not refs.records and not refs.links:
        return None
    out: dict[str, Any] = {
        "records": [list(ref) for ref in refs.records],
        "links": [list(link) for link in refs.links],
    }
    if refs.truncated:
        out["truncated"] = True
    return out


def proposal_of(document: Any) -> str | None:
    """The id of the Proposal a write response names (`proposal.id` or `proposal_id`)."""
    if not isinstance(document, Mapping):
        return None
    proposal = document.get("proposal")
    if isinstance(proposal, Mapping) and isinstance(proposal.get("id"), str):
        return str(proposal["id"])
    if isinstance(document.get("proposal_id"), str):
        return str(document["proposal_id"])
    return None


def parse_json(raw: bytes) -> Any:
    """A JSON body, or None when it is not JSON."""
    return _parse(raw) if raw else None
