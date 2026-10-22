"""The read-only tools of the exploration agent, each one Keeper read over the client.

There is no write tool and no tool that reaches anything but Keeper (no URL fetching): the
catalogue is closed and every argument is validated before a request is made. Results are
reduced to compact JSON (bounded items and value lengths) and every Record ref they contain is
remembered, so that the final answer may cite only Records the caller was actually shown.
RBAC, masking and audit are Keeper's: each call carries the caller's own credential.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lorebase_client import AsyncLorebaseClient
from lorebase_client import models as m
from lorebase_client.errors import LorebaseError
from lorebase_guide.embed import Embedder
from lorebase_guide.settings import AgentSettings
from lorebase_inference import InferenceError

JsonDict = dict[str, Any]
_TEXT_LIMIT = 500
_MAX_ATTRIBUTES = 40
_MAX_PATH = 4
_MAX_SEEDS = 5


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


RefText = Annotated[str, Field(min_length=3, max_length=300, description="Record ref `Kind/key`.")]


class SchemaArgs(ToolArgs):
    kinds: list[str] | None = Field(default=None, max_length=20)


class SearchArgs(ToolArgs):
    q: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    kinds: list[str] | None = Field(default=None, max_length=10)
    mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    limit: int = Field(default=10, ge=1, le=50)


class QueryArgs(ToolArgs):
    kind: str = Field(min_length=1, max_length=128)
    where: dict[str, Any] | None = None
    select: list[str] | None = Field(default=None, max_length=40)
    order_by: list[dict[str, str]] | None = Field(default=None, max_length=5)
    aggregate: dict[str, Any] | None = None
    limit: int = Field(default=10, ge=1, le=50)


class RefArgs(ToolArgs):
    ref: RefText


class LinksArgs(ToolArgs):
    ref: RefText
    relation: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=50)


class NeighborhoodArgs(ToolArgs):
    refs: list[str] = Field(min_length=1, max_length=_MAX_SEEDS)
    depth: int = Field(default=1, ge=1, le=5)
    relations: list[str] | None = Field(default=None, max_length=20)
    max_nodes: int = Field(default=30, ge=1, le=500)


class TraverseHopArgs(ToolArgs):
    relation: str = Field(min_length=1, max_length=128)
    where: dict[str, Any] | None = None
    limit: int | None = Field(default=None, ge=1, le=50)


class TraverseArgs(ToolArgs):
    start: RefText
    path: list[TraverseHopArgs] = Field(min_length=1, max_length=_MAX_PATH)


class HistoryArgs(ToolArgs):
    ref: RefText
    limit: int = Field(default=5, ge=1, le=20)


class ContextArgs(ToolArgs):
    refs: list[str] | None = Field(default=None, max_length=_MAX_SEEDS)
    search: str | None = Field(default=None, min_length=1, max_length=_TEXT_LIMIT)
    budget_tokens: int = Field(default=1_500, ge=200, le=20_000)
    depth: int = Field(default=1, ge=1, le=3)


@dataclass
class ToolResult:
    """What a tool shows the model (`data`), the user (`summary`) and the refs it showed."""

    ok: bool
    summary: str
    data: Any
    refs: list[str] = field(default_factory=list)


@dataclass
class ToolContext:
    """Everything a tool call may use; bound to one exploration."""

    client: AsyncLorebaseClient
    domain: str
    branch: str | None
    settings: AgentSettings
    embedder: Embedder | None
    seen: set[str] = field(default_factory=set)
    _search_info: m.SchemaSearch | None = None

    def clip(self, value: Any) -> Any:
        """A JSON value with long strings cut and long lists shortened."""
        limit = self.settings.max_value_chars
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit] + "..."
        if isinstance(value, list):
            items = [self.clip(v) for v in value[: self.settings.max_items]]
            if len(value) > len(items):
                items.append(f"... {len(value) - len(items)} more")
            return items
        if isinstance(value, dict):
            return {str(k): self.clip(v) for k, v in list(value.items())[:_MAX_ATTRIBUTES]}
        return value

    def record(self, item: m.RecordItem, *, with_values: bool = True) -> JsonDict:
        ref = f"{item.kind}/{item.key}"
        self.seen.add(ref)
        out: JsonDict = {"ref": ref}
        if item.title:
            out["title"] = self.clip(item.title)
        if with_values:
            out["values"] = self.clip(dict(item.values))
        if item.links:
            out["links"] = {link.relation: link.count for link in item.links}
        return out

    def ref_of(self, kind: str, key: str) -> str:
        ref = f"{kind}/{key}"
        self.seen.add(ref)
        return ref

    async def search_info(self) -> m.SchemaSearch:
        if self._search_info is None:
            document = await self.client.schema.get(self.domain, branch=self.branch)
            self._search_info = document.search
        return self._search_info


class ToolInputError(ValueError):
    """Arguments the tool cannot use; reported to the model, never sent to Keeper."""


def split_ref(ref: str) -> tuple[str, str]:
    kind, sep, key = ref.partition("/")
    if not sep or kind in (".", "..") or key in ("", ".", "..") or not kind:
        raise ToolInputError(f"ref must be `Kind/key`, got {ref[:80]!r}")
    return kind, key


def _refs(items: Sequence[str]) -> list[str]:
    return [item for item in items if item]


async def _schema(ctx: ToolContext, args: SchemaArgs) -> ToolResult:
    document = await ctx.client.schema.get(ctx.domain, branch=ctx.branch)
    content: Mapping[str, Any] = document.schema_
    names = {
        element_id: element.get("api_name", element_id)
        for section in ("kinds", "relations")
        for element_id, element in (content.get(section) or {}).items()
    }
    wanted = set(args.kinds or ())
    kinds: list[JsonDict] = []
    for kind in (content.get("kinds") or {}).values():
        if wanted and kind.get("api_name") not in wanted:
            continue
        attributes = {
            a.get("api_name", a_id): str((a.get("type") or {}).get("name", "?"))
            for a_id, a in (kind.get("attributes") or {}).items()
        }
        kinds.append({"kind": kind.get("api_name"), "attributes": ctx.clip(attributes)})
    relations = [
        {
            "relation": r.get("api_name"),
            "inverse": r.get("inverse_api_name"),
            "from": names.get(r.get("from_kind"), r.get("from_kind")),
            "to": names.get(r.get("to_kind"), r.get("to_kind")),
        }
        for r in (content.get("relations") or {}).values()
        if not wanted
        or names.get(r.get("from_kind")) in wanted
        or names.get(r.get("to_kind")) in wanted
    ]
    data = {
        "kinds": kinds[: ctx.settings.max_items],
        "relations": relations[: ctx.settings.max_items],
        "search_modes": list(document.search.modes),
    }
    ctx._search_info = document.search
    return ToolResult(True, f"{len(kinds)} Kinds, {len(relations)} Relations", data)


async def _search(ctx: ToolContext, args: SearchArgs) -> ToolResult:
    mode = args.mode
    vector: list[float] | None = None
    note = ""
    if mode != "lexical":
        info = await ctx.search_info()
        if mode not in info.modes or info.embedding is None or ctx.embedder is None:
            mode, note = "lexical", "semantic search unavailable here; used lexical"
        else:
            try:
                embedded = await ctx.embedder.embed(args.q, "query")
            except InferenceError:
                mode, note = "lexical", "query embedding failed; used lexical"
            else:
                if embedded.model != info.embedding.model or embedded.dims != info.embedding.dims:
                    mode, note = "lexical", "embedding model differs from Keeper's; used lexical"
                else:
                    vector = embedded.vector
    request = m.SearchRequest(
        branch=ctx.branch,
        q=args.q,
        kinds=args.kinds,
        mode=mode,
        query_vector=vector,
        limit=min(args.limit, ctx.settings.max_items),
    )
    found = await ctx.client.records.search(ctx.domain, request)
    hits = [
        {
            "ref": ctx.ref_of(hit.kind, hit.key),
            "title": ctx.clip(hit.title),
            "snippet": ctx.clip(hit.snippet),
            "score": round(hit.score, 4),
        }
        for hit in found.items
    ]
    data: JsonDict = {"mode": found.mode, "hits": hits}
    if note:
        data["note"] = note
    summary = f"{len(hits)} hits ({found.mode})"
    return ToolResult(True, summary, data, [h["ref"] for h in hits])


async def _query(ctx: ToolContext, args: QueryArgs) -> ToolResult:
    request = m.QueryRequest(
        kind=args.kind,
        where=args.where,
        select=args.select,
        order_by=args.order_by,
        aggregate=args.aggregate,
        limit=min(args.limit, ctx.settings.max_items),
        branch=ctx.branch,
    )
    found = await ctx.client.records.query(ctx.domain, request)
    records = [ctx.record(item) for item in found.items]
    data: JsonDict = {"records": records, "more": found.next_cursor is not None}
    if found.aggregate is not None:
        data["aggregate"] = ctx.clip(found.aggregate.model_dump(mode="json"))
    return ToolResult(True, f"{len(records)} Records", data, [r["ref"] for r in records])


async def _get(ctx: ToolContext, args: RefArgs) -> ToolResult:
    kind, key = split_ref(args.ref)
    found = await ctx.client.records.get(ctx.domain, kind, key, branch=ctx.branch, links=True)
    record = ctx.record(found.record)
    return ToolResult(True, f"Record {record['ref']}", record, [record["ref"]])


async def _links(ctx: ToolContext, args: LinksArgs) -> ToolResult:
    kind, key = split_ref(args.ref)
    page = await ctx.client.records.links(
        ctx.domain,
        kind,
        key,
        args.relation,
        branch=ctx.branch,
        limit=min(args.limit, ctx.settings.max_items),
    )
    if page.items:
        ctx.seen.add(args.ref)
    links: list[JsonDict] = []
    for item in page.items:
        entry: JsonDict = {"relation": item.relation, "direction": item.direction}
        if item.target is not None:
            entry["target"] = ctx.ref_of(item.target.kind, item.target.key)
        else:
            entry["to_key"] = item.to_key
        if item.values:
            entry["values"] = ctx.clip(dict(item.values))
        links.append(entry)
    refs = _refs([str(link.get("target", "")) for link in links])
    data = {"links": links, "more": page.next_cursor is not None}
    return ToolResult(True, f"{len(links)} links of {args.ref}", data, refs)


async def _neighborhood(ctx: ToolContext, args: NeighborhoodArgs) -> ToolResult:
    seeds = [m.RecordAddress(kind=k, key=v) for k, v in (split_ref(r) for r in args.refs)]
    request = m.NeighborhoodRequest(
        branch=ctx.branch,
        seeds=seeds,
        depth=min(args.depth, ctx.settings.max_depth),
        relations=args.relations,
        max_nodes=min(args.max_nodes, ctx.settings.max_nodes),
    )
    found = await ctx.client.records.neighborhood(ctx.domain, request)
    nodes = [ctx.record(item) for item in found.nodes[: ctx.settings.max_items]]
    edges = [
        {
            "relation": e.relation,
            "from": ctx.ref_of(e.source.kind, e.source.key),
            "to": ctx.ref_of(e.to.kind, e.to.key),
        }
        for e in found.edges[: ctx.settings.max_items * 2]
    ]
    data = {"nodes": nodes, "edges": edges, "truncated": found.truncated}
    summary = f"{len(found.nodes)} nodes, {len(found.edges)} edges"
    return ToolResult(True, summary, data, [n["ref"] for n in nodes])


async def _traverse(ctx: ToolContext, args: TraverseArgs) -> ToolResult:
    kind, key = split_ref(args.start)
    request = m.TraverseRequest(
        branch=ctx.branch,
        start=m.RecordAddress(kind=kind, key=key),
        path=[
            m.TraverseStep(
                relation=hop.relation,
                where=hop.where,
                limit=min(hop.limit or ctx.settings.max_items, ctx.settings.max_items),
            )
            for hop in args.path
        ],
    )
    found = await ctx.client.records.traverse(ctx.domain, request)
    start = ctx.record(found.start)
    hops: list[JsonDict] = []
    refs = [str(start["ref"])]
    for hop in found.hops:
        records = [ctx.record(r) for r in hop.records[: ctx.settings.max_items]]
        refs.extend(str(r["ref"]) for r in records)
        hops.append({"relation": hop.relation, "records": records, "truncated": hop.truncated})
    summary = f"{len(hops)} hops, {len(refs) - 1} Records"
    return ToolResult(True, summary, {"start": start, "hops": hops}, refs)


async def _history(ctx: ToolContext, args: HistoryArgs) -> ToolResult:
    kind, key = split_ref(args.ref)
    page = await ctx.client.records.history(
        ctx.domain, kind, key, branch=ctx.branch, values=True, limit=args.limit
    )
    ref = ctx.ref_of(kind, key) if page.items else f"{kind}/{key}"
    changes = [
        {
            "at": change.at.isoformat(),
            "change": change.change,
            "author": change.author,
            "message": ctx.clip(change.message),
            "attributes": ctx.clip([a.model_dump(mode="json") for a in change.attributes]),
        }
        for change in page.items
    ]
    refs = [ref] if changes else []
    return ToolResult(True, f"{len(changes)} changes of {ref}", {"changes": changes}, refs)


async def _lineage(ctx: ToolContext, args: RefArgs) -> ToolResult:
    kind, key = split_ref(args.ref)
    found = await ctx.client.records.lineage(ctx.domain, kind, key, branch=ctx.branch)
    shown = found.created is not None or bool(found.attributes)
    ref = ctx.ref_of(kind, key) if shown else f"{kind}/{key}"
    data = {
        "ref": ref,
        "changes": found.changes,
        "sources": ctx.clip(list(found.sources)),
        "attributes": ctx.clip([a.model_dump(mode="json") for a in found.attributes]),
    }
    return ToolResult(True, f"lineage of {ref}", data, [ref] if shown else [])


async def _context(ctx: ToolContext, args: ContextArgs) -> ToolResult:
    if not args.refs and not args.search:
        raise ToolInputError("give refs or search")
    for ref in args.refs or ():
        split_ref(ref)
    request = m.ContextRequest(
        branch=ctx.branch,
        refs=args.refs,
        search=args.search,
        budget_tokens=min(args.budget_tokens, ctx.settings.max_context_tokens),
        depth=min(args.depth, ctx.settings.max_depth),
        format="json",
    )
    pack = await ctx.client.agents.context(ctx.domain, request)
    refs: list[str] = []
    for entry in [*pack.seeds, *pack.neighbors]:
        kind, key = entry.get("kind"), entry.get("key")
        if isinstance(kind, str) and isinstance(key, str):
            refs.append(ctx.ref_of(kind, key))
    data = {"seeds": ctx.clip(pack.seeds), "neighbors": ctx.clip(pack.neighbors)}
    summary = f"{len(pack.seeds)} seeds, {len(pack.neighbors)} neighbors"
    return ToolResult(True, summary, data, refs)


Runner = Callable[[ToolContext, Any], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[ToolArgs]
    run: Runner


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            "schema",
            "Kinds with their attributes and the Relations between them. args: {kinds?: [Kind]}",
            SchemaArgs,
            _schema,
        ),
        Tool(
            "search",
            "Find Records by text. args: {q: text, kinds?: [Kind], mode?: lexical|semantic|"
            "hybrid, limit?: 1-50}",
            SearchArgs,
            _search,
        ),
        Tool(
            "query",
            "Filter Records of one Kind with the query DSL. args: {kind, where?: {attr: value | "
            "{op: value}}, select?: [attr], order_by?: [{attr: asc|desc}], aggregate?: {...}, "
            "limit?: 1-50}",
            QueryArgs,
            _query,
        ),
        Tool("get", "One Record with link counts. args: {ref: Kind/key}", RefArgs, _get),
        Tool(
            "links",
            "Links of a Record along one Relation. args: {ref, relation, limit?}",
            LinksArgs,
            _links,
        ),
        Tool(
            "neighborhood",
            "Records around seed Records. args: {refs: [Kind/key], depth?: 1-2, relations?, "
            "max_nodes?}",
            NeighborhoodArgs,
            _neighborhood,
        ),
        Tool(
            "traverse",
            "Follow Relations from a Record. args: {start: Kind/key, path: [{relation, where?, "
            "limit?}]}",
            TraverseArgs,
            _traverse,
        ),
        Tool("history", "Recent changes of a Record. args: {ref, limit?}", HistoryArgs, _history),
        Tool("lineage", "Where a Record's values came from. args: {ref}", RefArgs, _lineage),
        Tool(
            "context",
            "A context pack around refs or a search. args: {refs?: [Kind/key], search?: text, "
            "budget_tokens?, depth?}",
            ContextArgs,
            _context,
        ),
    )
}


def _keeper_error(exc: LorebaseError) -> str:
    if exc.status == 403:
        return "permission denied: this caller may not read that"
    if exc.status == 404:
        return "not found (or not visible to this caller)"
    if exc.status in (400, 422):
        problems = "; ".join(
            f"{e.get('pointer', '')}: {e.get('message', '')}" for e in exc.errors[:5]
        )
        return f"invalid request: {problems or exc.detail or exc.title or 'rejected'}"[:400]
    if exc.status == 429:
        return "rate limited by Keeper"
    return f"Keeper error {exc.status or 'unreachable'}"


async def run_tool(ctx: ToolContext, name: str, raw_args: Any) -> ToolResult:
    """Validate and run one tool call; failures become results the model can read."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(False, f"unknown tool {name[:40]!r}", {"error": "unknown tool"})
    try:
        args = tool.args.model_validate(raw_args if isinstance(raw_args, dict) else {})
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}" for e in exc.errors()
        ][:5]
        return ToolResult(
            False, "invalid arguments", {"error": "invalid arguments", "problems": problems}
        )
    try:
        return await tool.run(ctx, args)
    except ValueError as exc:  # ToolInputError, and path segments the client refuses
        return ToolResult(False, "invalid arguments", {"error": str(exc)[:200]})
    except LorebaseError as exc:
        message = _keeper_error(exc)
        return ToolResult(False, message, {"error": message})
